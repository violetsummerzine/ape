import time
from enum import Enum, IntEnum
from pathlib import Path
from typing import Dict, Iterator, List, Optional

from dataclassy import as_dict
from eth_typing import HexStr
from eth_utils import add_0x_prefix
from hexbytes import HexBytes
from tqdm import tqdm  # type: ignore
from web3 import Web3

from ape.exceptions import ProviderError, TransactionError
from ape.logging import logger
from ape.types import BlockID, TransactionSignature

from . import networks
from .base import abstractdataclass, abstractmethod
from .config import ConfigItem


class TransactionType(Enum):
    STATIC = "0x0"
    DYNAMIC = "0x2"  # EIP-1559


@abstractdataclass
class TransactionAPI:
    """
    An API class representing a Transaction.
    """

    chain_id: int = 0
    sender: str = ""
    receiver: str = ""
    nonce: Optional[int] = None  # NOTE: `Optional` only to denote using default behavior
    value: int = 0
    gas_limit: Optional[int] = None  # NOTE: `Optional` only to denote using default behavior
    data: bytes = b""
    type: TransactionType = TransactionType.STATIC

    # If left as None, will get set to the network's default required confirmations.
    required_confirmations: Optional[int] = None

    signature: Optional[TransactionSignature] = None

    def __post_init__(self):
        if not self.is_valid:
            raise ProviderError("Transaction is not valid.")

    @property
    def max_fee(self) -> int:
        """
        The total amount in fees willing to be spent on a transaction.
        Override this property as needed, such as for EIP-1559 differences.

        See :class:`~ape_ethereum.ecosystem.StaticFeeTransaction` and
        :class`~ape_ethereum.ecosystem.DynamicFeeTransaction` as examples.
        """
        return 0

    @max_fee.setter
    def max_fee(self, value: int):
        """
        Set the max fee.
        Must be overriden or else raises `NotImplementedError`.

        Args:
            value (int): The number of the fee
        """
        raise NotImplementedError("Max fee is not settable by default.")

    @property
    def total_transfer_value(self) -> int:
        """
        The total amount of WEI that a transaction could use.
        Useful for determining if an account balance can afford
        to submit the transaction.
        """
        return self.value + self.max_fee

    @property
    @abstractmethod
    def is_valid(self):
        ...

    @abstractmethod
    def encode(self) -> bytes:
        """
        Take this object and produce a hash to sign to submit a transaction
        """

    def as_dict(self) -> dict:
        """
        Create a ``dict`` representation of the transaction.

        Returns:
            dict
        """
        return as_dict(self)

    def __repr__(self) -> str:
        data = as_dict(self)  # NOTE: `as_dict` could be overridden
        params = ", ".join(f"{k}={v}" for k, v in data.items())
        return f"<{self.__class__.__name__} {params}>"

    def __str__(self) -> str:
        """
        Convert `dict` to `HexBytes`
        """
        data = as_dict(self)  # NOTE: `as_dict` could be overridden
        if len(data["data"]) > 9:
            data["data"] = (
                "0x" + bytes(data["data"][:3]).hex() + "..." + bytes(data["data"][-3:]).hex()
            )
        else:
            data["data"] = "0x" + bytes(data["data"]).hex()
        params = "\n  ".join(f"{k}: {v}" for k, v in data.items())
        return f"{self.__class__.__name__}:\n  {params}"


class TransactionStatusEnum(IntEnum):
    FAILING = 0
    NO_ERROR = 1


class ConfirmationsProgressBar:
    """
    A progress bar tracking the confirmations of a transaction.
    """

    def __init__(self, confirmations: int):
        self._req_confs = confirmations
        self._bar = tqdm(range(confirmations))
        self._confs = 0

    def __enter__(self):
        self._update_bar(0)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._bar.close()

    @property
    def confs(self) -> int:
        """
        The number of confirmations that have occurred.

        Returns:
            int: The total number of confirmations that have occurred.
        """
        return self._confs

    @confs.setter
    def confs(self, new_value):
        if new_value == self._confs:
            return

        diff = new_value - self._confs
        self._confs = new_value
        self._update_bar(diff)

    def _update_bar(self, amount: int):
        self._set_description()
        self._bar.update(amount)
        self._bar.refresh()

    def _set_description(self):
        self._bar.set_description(f"Confirmations ({self._confs}/{self._req_confs})")


@abstractdataclass
class ReceiptAPI:
    provider: "ProviderAPI"
    txn_hash: str
    status: TransactionStatusEnum
    block_number: int
    gas_used: int
    gas_price: int
    logs: List[dict] = []
    contract_address: Optional[str] = None
    required_confirmations: int = 0
    sender: str
    nonce: int

    def __post_init__(self):
        txn_hash = self.txn_hash.hex() if isinstance(self.txn_hash, HexBytes) else self.txn_hash
        logger.info(f"Submitted {txn_hash} (gas_used={self.gas_used})")

    def __str__(self) -> str:
        return f"<{self.__class__.__name__} {self.txn_hash}>"

    def raise_for_status(self, txn: TransactionAPI):
        """
        Handle provider-specific errors regarding a non-successful
        :class:`~api.providers.TransactionStatusEnum`.
        """

    def ran_out_of_gas(self, gas_limit: int) -> bool:
        """
        Returns ``True`` when the transaction failed and used the
        same amount of gas as the given ``gas_limit``.
        """
        return self.status == TransactionStatusEnum.FAILING and self.gas_used == gas_limit

    @classmethod
    @abstractmethod
    def decode(cls, data: dict) -> "ReceiptAPI":
        """
        Converts a dictionary to :class:`~ape.api.ReceiptAPI`.

        Args:
            data (dict): The dict to convert.

        Returns:
            :class:`~ape.api.ReceiptAPI`
        """

    def await_confirmations(self) -> "ReceiptAPI":
        """
        Waits for a transaction to be considered confirmed.

        Returns:
            :class:`~ape.api.ReceiptAPI`: The receipt that is now confirmed.
        """
        # Wait for nonce from provider to increment.
        sender_nonce = self.provider.get_nonce(self.sender)
        while sender_nonce == self.nonce:  # type: ignore
            time.sleep(1)
            sender_nonce = self.provider.get_nonce(self.sender)

        if self.required_confirmations == 0:
            # The transaction might not yet be confirmed but
            # the user is aware of this. Or, this is a development environment.
            return self

        confirmations_occurred = 0

        with ConfirmationsProgressBar(self.required_confirmations) as progress_bar:
            while confirmations_occurred < self.required_confirmations:
                latest_block = self.provider.get_block("latest")
                confirmations_occurred = latest_block.number - self.block_number  # type: ignore
                progress_bar.confs = confirmations_occurred

                if confirmations_occurred == self.required_confirmations:
                    break

                time.sleep(5)

        return self


@abstractdataclass
class BlockGasAPI:
    """
    An abstract class for representing gas data for a block.
    """

    gas_limit: int
    gas_used: int
    base_fee: Optional[int] = None

    @classmethod
    @abstractmethod
    def decode(cls, data: Dict) -> "BlockGasAPI":
        """
        Convert a dictionary to a :class:`~ape.api.BlockGasAPI`.

        Args:
            data (dict): The dictionary to convert.

        Returns:
            :class:`~ape.api.BlockGasAPI`
        """


@abstractdataclass
class BlockConsensusAPI:
    """
    An abstract class representing the consensus data for a block.
    """

    difficulty: Optional[int] = None
    total_difficulty: Optional[int] = None

    @classmethod
    @abstractmethod
    def decode(cls, data: Dict) -> "BlockConsensusAPI":
        """
        Convert a dictionary to a :class:`~ape.api.BlockConsensusAPI`.

        Args:
            data (dict): The dictionary to convert.

        Returns:
            :class:`~ape.api.BlockConsensusAPI`
        """


@abstractdataclass
class BlockAPI:
    """
    An abstract class representing a block.
    """

    gas_data: BlockGasAPI
    consensus_data: BlockConsensusAPI
    hash: HexBytes
    number: int
    parent_hash: HexBytes
    size: int
    timestamp: float

    @classmethod
    @abstractmethod
    def decode(cls, data: Dict) -> "BlockAPI":
        """
        Converts a dictionary to :class:`~ape.api.BlockAPI`.

        Args:
            data (dict): Dictionary to convert.

        Returns:
            :class:`~ape.api.BlockAPI`
        """


@abstractdataclass
class ProviderAPI:
    """
    A Provider must work with a particular Network in a particular Ecosystem
    """

    name: str  # Plugin name
    network: networks.NetworkAPI
    config: ConfigItem
    provider_settings: dict
    data_folder: Path
    request_header: str

    @abstractmethod
    def connect(self):
        """
        Connect a contract to a provider.
        """

    @abstractmethod
    def disconnect(self):
        """
        Disconnect a contract from a provider.
        """

    @abstractmethod
    def update_settings(self, new_settings: dict):
        """
        Change the settings connection protocol.
        May require a reconnect.

        Agrs:
            new_settings (dict): Value of new provider
        """

    @property
    @abstractmethod
    def chain_id(self) -> int:
        """
        Gives value of blockchain id.

        Returns:
            int: value of blockchain id.
        """

    @abstractmethod
    def get_balance(self, address: str) -> int:
        """
        Gives values of address balance.

        Args:
            address (str): Address string.

        Returns:
            int: value of balance at the address.
        """

    @abstractmethod
    def get_code(self, address: str) -> bytes:
        """
        Get the bytes of the contract.

        Args:
            address (str): value of address (Required)

        Returns:
        bytes (int): HexBytes of contract.
        """

    @abstractmethod
    def get_nonce(self, address: str) -> int:
        """
        Gets the number of transactions.

        Agrs:
            address (str): string of address.

        Returns:
            int: value of nonce.
        """

    @abstractmethod
    def estimate_gas_cost(self, txn: TransactionAPI) -> int:
        """
        Use for setting the default gas limit on a transaction.

        Args:
            txn (:class:`~ape.api.providers.TransactionAPI`) :
            The transaction to estimate the gas for.

        Returns:
            int: value of estimated gas cost.
        """

    @property
    @abstractmethod
    def gas_price(self) -> int:
        """
        Value of gas.

        Returns:
            int
        """

    @property
    def priority_fee(self) -> int:
        """
         A miner tip to incentivize them
        to include your transaction in a block.

        Returns:
            int: Value of fee.
        """
        raise NotImplementedError("priority_fee is not implemented by this provider")

    @property
    def base_fee(self) -> int:
        """
        Value of the fee determined by the network.
        Must overrride address or else it will raise `NotImplementedError`.

        Returns:
            int
        """
        raise NotImplementedError("base_fee is not implemented by this provider")

    @abstractmethod
    def get_block(self, block_id: BlockID) -> BlockAPI:
        """
        Gets blockchain id.
        Must override block_id or else rasise `error`.

        Args:
            block_id (`int`): Value of Block Id.

        Returns:
            :class:~`ape.types.BlockID`: The block for the given ID.
        """

    @abstractmethod
    def send_call(self, txn: TransactionAPI) -> bytes:  # Return value of function
        """
        Executes a new transaction call immediately without creating a
        transaction on the block chain.

        Returns:
            str: The result of the transaction call.
        """

    @abstractmethod
    def get_transaction(self, txn_hash: str) -> ReceiptAPI:
        """
        Returns the information about a transaction requested by transaction hash.

        Params:
            txn_hash (str): The hash of the transaction to retrieve.

        Returns:
            class:~`api.providers.ReceiptAPI`:
                The receipt of the transaction with the given hash.
        """

    @abstractmethod
    def send_transaction(self, txn: TransactionAPI) -> ReceiptAPI:
        ...

    @abstractmethod
    def get_events(self, **filter_params) -> Iterator[dict]:
        """
        Returns an array of all logs matching a given set of filter parameters.
        """


class TestProviderAPI(ProviderAPI):
    """
    An API for providers that have development functionality, such as snapshotting.
    """

    @abstractmethod
    def snapshot(self) -> str:
        ...

    @abstractmethod
    def revert(self, snapshot_id: str):
        ...


class Web3Provider(ProviderAPI):
    """
    A base provider that is web3 based.
    """

    _web3: Web3 = None  # type: ignore

    def update_settings(self, new_settings: dict):
        """
        Update the provider settings and re-connect.
        """
        self.disconnect()
        self.provider_settings.update(new_settings)
        self.connect()

    def estimate_gas_cost(self, txn: TransactionAPI) -> int:
        """
        Generates and returns an estimate of how much gas is necessary
        to allow the transaction to complete.
        The transaction will not be added to the blockchain.
        """
        txn_dict = txn.as_dict()
        return self._web3.eth.estimate_gas(txn_dict)  # type: ignore

    @property
    def chain_id(self) -> int:
        """
        Returns the currently configured chain ID,
        a value used in replay-protected transaction signing as introduced by EIP-155.
        """
        return self._web3.eth.chain_id

    @property
    def gas_price(self) -> int:
        """
        Returns the current price per gas in wei.
        """
        return self._web3.eth.generate_gas_price()  # type: ignore

    @property
    def priority_fee(self) -> int:
        """
        Returns the current max priority fee per gas in wei.
        """
        return self._web3.eth.max_priority_fee

    @property
    def base_fee(self) -> int:
        """
        Returns the current base fee from the latest block.

        NOTE: If your chain does not support base_fees (EIP-1559),
        this method will raise a not-implemented error.
        """
        block = self.get_block("latest")

        if block.gas_data.base_fee is None:
            # Non-EIP-1559 chains or we time-travelled pre-London fork.
            raise NotImplementedError("base_fee is not implemented by this provider.")

        return block.gas_data.base_fee

    def get_block(self, block_id: BlockID) -> BlockAPI:
        """
        Returns a block for the given ID.

        Args:
            block_id: The ID of the block to get. Set as
              "latest" to get the latest block,
              "earliest" to get the earliest block,
              "pending" to get the pending block,
              or pass in a block number or hash.

        Returns:
            The block for the given block ID.
        """
        if isinstance(block_id, str):
            block_id = HexStr(block_id)

            if block_id.isnumeric():
                block_id = add_0x_prefix(block_id)

        block_data = self._web3.eth.get_block(block_id)
        return self.network.ecosystem.block_class.decode(block_data)  # type: ignore

    def get_nonce(self, address: str) -> int:
        """
        Returns the number of transactions sent from an address.
        """
        return self._web3.eth.get_transaction_count(address)  # type: ignore

    def get_balance(self, address: str) -> int:
        """
        Returns the balance of the account of a given address.
        """
        return self._web3.eth.get_balance(address)  # type: ignore

    def get_code(self, address: str) -> bytes:
        """
        Returns code at a given address.
        """
        return self._web3.eth.get_code(address)  # type: ignore

    def send_call(self, txn: TransactionAPI) -> bytes:
        """
        Executes a new message call immediately without creating a
        transaction on the block chain.
        """
        return self._web3.eth.call(txn.as_dict())

    def get_transaction(self, txn_hash: str, required_confirmations: int = 0) -> ReceiptAPI:
        """
        Returns the information about a transaction requested by transaction hash.

        Params:
            txn_hash (str): The hash of the transaction to retrieve.
            required_confirmations (int): If more than 0, waits for that many
                confirmations before returning the receipt. This is to increase confidence
                that your transaction is in its final position on the blockchain. Defaults
                to 0.

        Returns:
            The receipt of the transaction with the given hash.
        """
        if required_confirmations < 0:
            raise TransactionError(message="Required confirmations cannot be negative.")

        receipt_data = self._web3.eth.wait_for_transaction_receipt(HexBytes(txn_hash))
        txn = self._web3.eth.get_transaction(txn_hash)  # type: ignore
        receipt = self.network.ecosystem.receipt_class.decode(
            {
                "provider": self,
                "required_confirmations": required_confirmations,
                **txn,
                **receipt_data,
            }
        )
        return receipt.await_confirmations()

    def get_events(self, **filter_params) -> Iterator[dict]:
        """
        Returns an array of all logs matching a given set of filter parameters.
        """
        return iter(self._web3.eth.get_logs(filter_params))  # type: ignore

    def send_transaction(self, txn: TransactionAPI) -> ReceiptAPI:
        txn_hash = self._web3.eth.send_raw_transaction(txn.encode())
        req_confs = (
            txn.required_confirmations
            if txn.required_confirmations is not None
            else self.network.required_confirmations
        )
        receipt = self.get_transaction(txn_hash.hex(), required_confirmations=req_confs)
        return receipt


class UpstreamProvider(ProviderAPI):
    """
    A provider that can also be set as another provider's upstream.
    """

    @property
    @abstractmethod
    def connection_str(self) -> str:
        """
        The str used by downstream providers to connect to this one.
        For example, the URL for HTTP-based providers.
        """

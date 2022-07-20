from abc import ABC, abstractmethod
from collections.abc import Collection, Hashable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from gzip import open as gzopen
from pickle import dump, load
from typing import Generic, NewType, Optional, TypeVar, Union, overload

from pickaxe_generic.containers import DataUnitGen
from pickaxe_generic.datatypes import (
    DataUnit,
    Identifier,
    MolDatBase,
    OpDatBase,
)

_MolIndex = NewType("_MolIndex", int)
_OpIndex = NewType("_OpIndex", int)
_RxnIndex = NewType("_RxnIndex", int)
_I_T = TypeVar("_I_T", bound=int)
_ID_T = TypeVar("_ID_T", bound=Identifier)


@dataclass(frozen=True, order=True, slots=True)
class Reaction:
    operator: _OpIndex
    reactants: tuple[_MolIndex, ...]
    products: tuple[_MolIndex, ...]


@dataclass(frozen=True)
class __ValueQueryData(Generic[DataUnitGen, _I_T]):
    __slots__ = ("_list", "_map")
    _list: Sequence[DataUnitGen]
    _map: Mapping[Identifier, _I_T]

    def __contains__(self, item: Union[Identifier, DataUnitGen]) -> bool:
        if isinstance(item, DataUnit):
            item = item.uid
        return item in self._map.keys()

    @overload
    def __getitem__(self, item: slice) -> Sequence[DataUnitGen]:
        ...

    @overload
    def __getitem__(self, item: Union[_I_T, Identifier]) -> DataUnitGen:
        ...

    def __getitem__(self, item: Union[slice, _I_T, Identifier]):
        if isinstance(item, slice):
            return self._list[item]
        if isinstance(item, int):
            return self._list[item]
        return self._list[self._map[item]]

    def i(self, uid: Identifier) -> _I_T:
        return self._map[uid]

    def keys(self) -> Collection[Identifier]:
        return self._map.keys()

    def uid(self, i: _I_T) -> Identifier:
        return self._list[i].uid

    def __len__(self) -> int:
        return len(self._list)

    def __iter__(self) -> Iterator[DataUnitGen]:
        return iter(self._list)


@dataclass(frozen=True)
class __ValueQueryAssoc(Generic[_ID_T, _I_T]):
    __slots__ = ("_list", "_map")
    _list: Sequence[_ID_T]
    _map: Mapping[_ID_T, _I_T]

    @overload
    def __getitem__(self, item: slice) -> Sequence[_ID_T]:
        ...

    @overload
    def __getitem__(self, item: _I_T) -> _ID_T:
        ...

    def __getitem__(self, item: Union[slice, _I_T]):
        if isinstance(item, slice):
            return self._list[item]
        return self._list[item]

    def i(self, item: _ID_T) -> _I_T:
        return self._map[item]

    def __len__(self) -> int:
        return len(self._list)

    def __iter__(self) -> Iterator[_ID_T]:
        return iter(self._list)


class ChemNetwork(ABC):
    __slots__ = ()

    @abstractmethod
    def __init__(self) -> None:
        ...


class ChemNetworkBin(ChemNetwork):
    __slots__ = (
        "_mol_list",
        "_op_list",
        "_rxn_list",
        "_mol_map",
        "_op_map",
        "_rxn_map",
        "_mol_query",
        "_op_query",
        "_rxn_query",
    )

    def __init__(self) -> None:
        self._mol_list: list[MolDatBase] = []
        self._op_list: list[OpDatBase] = []
        self._rxn_list: list[Reaction] = []

        self._mol_map: dict[Identifier, _MolIndex] = {}
        self._op_map: dict[Identifier, _OpIndex] = {}
        self._rxn_map: dict[Reaction, _RxnIndex] = {}

        self._mol_meta: list[dict] = []
        self._op_meta: list[dict] = []
        self._rxn_meta: list[dict] = []

        self._mol_producers: Mapping[_MolIndex, Collection[_RxnIndex]] = {}
        self._mol_consumers: Mapping[_MolIndex, Collection[_RxnIndex]] = {}

        self._compat_table: list[Sequence[list[_MolIndex]]] = []

        self._mol_query: Optional[
            __ValueQueryData[MolDatBase, _MolIndex]
        ] = None
        self._op_query: Optional[__ValueQueryData[OpDatBase, _OpIndex]] = None
        self._rxn_query: Optional[__ValueQueryAssoc[Reaction, _RxnIndex]] = None

    @property
    def mols(self) -> __ValueQueryData[MolDatBase, _MolIndex]:
        if self._mol_query is None:
            self._mol_query = __ValueQueryData(self._mol_list, self._mol_map)
        return self._mol_query

    @property
    def ops(self) -> __ValueQueryData[OpDatBase, _OpIndex]:
        if self._op_query is None:
            self._op_query = __ValueQueryData(self._op_list, self._op_map)
        return self._op_query

    @property
    def rxns(self) -> __ValueQueryAssoc[Reaction, _RxnIndex]:
        if self._rxn_query is None:
            self._rxn_query = __ValueQueryAssoc(self._rxn_list, self._rxn_map)
        return self._rxn_query

    def mol_meta(self, index: int, key: Hashable, value=None):
        if value is None:
            return self._mol_meta[index][key]
        self._mol_meta[index][key] = value

    def op_meta(self, index: int, key: Hashable, value=None):
        if value is None:
            return self._op_meta[index][key]
        self._op_meta[index][key] = value

    def rxn_meta(self, index: int, key: Hashable, value=None):
        if value is None:
            return self._rxn_meta[index][key]
        self._rxn_meta[index][key] = value

    def compat_table(self, index: int) -> Sequence[Sequence[_MolIndex]]:
        return self._compat_table[index]

    def consumers(
        self, mol: Union[int, MolDatBase, Identifier]
    ) -> Collection[int]:
        if isinstance(mol, int):
            return self._mol_consumers[_MolIndex(mol)]
        elif isinstance(mol, MolDatBase):
            return self._mol_consumers[self._mol_map[mol.uid]]
        return self._mol_consumers[self._mol_map[mol]]

    def producers(
        self, mol: Union[int, MolDatBase, Identifier]
    ) -> Collection[int]:
        if isinstance(mol, int):
            return self._mol_producers[_MolIndex(mol)]
        elif isinstance(mol, MolDatBase):
            return self._mol_producers[self._mol_map[mol.uid]]
        return self._mol_producers[self._mol_map[mol]]

    def add_mol(
        self, mol: MolDatBase, meta: Optional[Mapping] = None
    ) -> _MolIndex:
        # if already in database, return existing index
        mol_uid = mol.uid
        if mol_uid in self._mol_map:
            return self._mol_map[mol_uid]

        # add mol to main mol list
        mol_index = _MolIndex(len(self._mol_list))
        self._mol_list.append(mol)

        # add mol id to UID mapping
        self._mol_map[mol_uid] = mol_index

        # add mol metadata to table
        if meta is None:
            self._mol_meta.append({})
        else:
            self._mol_meta.append(dict(meta))

        # test operator compatibility and add to table
        for i, op in enumerate(self.ops):
            for argnum in range(len(op)):
                if op.compat(mol, argnum):
                    self._compat_table[i][argnum].append(mol_index)

        return mol_index

    def add_op(self, op: OpDatBase, meta: Optional[Mapping] = None) -> _OpIndex:
        # if already in database, return existing index
        op_uid = op.uid
        if op_uid in self._op_map:
            return self._op_map[op_uid]

        # add op to main op list
        op_index = _OpIndex(len(self._op_list))
        self._op_list.append(op)

        # add op id to UID mapping
        self._op_map[op_uid] = op_index

        # add mol metadata to table
        if meta is None:
            self._op_meta.append({})
        else:
            self._op_meta.append(dict(meta))

        # test operator compatibility and add to table
        self._compat_table.append(
            tuple(
                [
                    [
                        _MolIndex(mol_index)
                        for mol_index, mol in enumerate(self._mol_list)
                        if op.compat(mol, argnum)
                    ]
                    for argnum in range(len(op))
                ]
            )
        )

        return op_index

    def add_rxn(
        self,
        op: _OpIndex,
        reactants: Sequence[_MolIndex],
        products: Sequence[_MolIndex],
        meta: Optional[Mapping] = None,
    ) -> _RxnIndex:
        rxn = Reaction(op, tuple(reactants), tuple(products))

        # if already in database, return existing index
        if rxn in self._rxn_map:
            return self._rxn_map[rxn]

        # sanity check that all reactants and products exist in the network
        if max(max(reactants), max(products)) >= len(self._mol_list):
            IndexError(
                f"One of the molecule components for reaction {rxn} is not in the network."
            )
        # sanity check that operator exists in the network
        if op >= len(self._op_list):
            IndexError(
                f"The operator for reaction {rxn} is not in the network."
            )

        # add rxn to main rxn list
        rxn_index = _RxnIndex(len(self._rxn_list))
        self._rxn_list.append(rxn)

        # add rxn to index mapping
        self._rxn_map[rxn] = rxn_index

        # add rxn metadata to table
        if meta is None:
            self._op_meta.append({})
        else:
            self._op_meta.append(dict(meta))

        return rxn_index


def dump_network_to_file(
    network: ChemNetwork, filepath: str = "network.dat"
) -> None:
    with gzopen(filepath, "wb") as fout:
        dump(network, fout)


def load_network_from_file(filepath: str = "network.dat") -> ChemNetwork:
    with gzopen(filepath, "rb") as fin:
        return load(fin)

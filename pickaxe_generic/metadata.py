import operator
from abc import ABC, abstractmethod
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from email.generator import Generator
from functools import reduce
from itertools import chain
from multiprocessing.sharedctypes import Value
from operator import add, or_
from typing import (
    Any,
    Callable,
    Generic,
    Hashable,
    Iterable,
    Optional,
    Protocol,
    TypeVar,
    Union,
    final,
)

from pickaxe_generic.datatypes import (
    DataPacket,
    DataPacketE,
    DataUnit,
    Identifier,
    MetaKeyPacket,
    MolDatBase,
    OpDatBase,
)
from pickaxe_generic.filters import ReactionFilterBase
from pickaxe_generic.network import ChemNetwork, ReactionExplicit
from pickaxe_generic.utils import logreduce


class MetaSink(Protocol):
    __slots__ = ()

    @abstractmethod
    @property
    def meta_required(self) -> MetaKeyPacket:
        ...


_T = TypeVar("_T")
_U = TypeVar("_U")
MetaDataResolverFunc = Callable[[_T, _T], _T]


class LocalPropertyCalc(ABC, Generic[_T]):
    @abstractmethod
    @property
    def key(self) -> Hashable:
        ...

    @abstractmethod
    @property
    def meta_required(self) -> MetaKeyPacket:
        ...

    @abstractmethod
    @property
    def resolver(self) -> MetaDataResolverFunc[_T]:
        ...


class MolPropertyCalc(LocalPropertyCalc[_T]):
    @abstractmethod
    def __call__(
        self, data: DataPacket[MolDatBase], prev_value: Optional[_T] = None
    ) -> Optional[_T]:
        ...


class MolPropertyFromRxnCalc(LocalPropertyCalc[_T]):
    @abstractmethod
    def __call__(
        self,
        data: DataPacket[MolDatBase],
        rxn: ReactionExplicit,
        prev_value: Optional[_T] = None,
    ) -> Optional[_T]:
        ...


class OpPropertyCalc(LocalPropertyCalc[_T]):
    @abstractmethod
    def __call__(
        self, data: DataPacket[OpDatBase], prev_value: Optional[_T] = None
    ) -> Optional[_T]:
        ...


class OpPropertyFromRxnCalc(LocalPropertyCalc[_T]):
    @abstractmethod
    def __call__(
        self,
        data: DataPacket[OpDatBase],
        rxn: ReactionExplicit,
        prev_value: Optional[_T] = None,
    ) -> Optional[_T]:
        ...


class RxnPropertyCalc(LocalPropertyCalc[_T]):
    @abstractmethod
    def __call__(
        self, data: ReactionExplicit, prev_value: Optional[_T] = None
    ) -> Optional[_T]:
        ...


@dataclass(frozen=True)
class KeyOutput:
    mol_keys: frozenset[Hashable]
    op_keys: frozenset[Hashable]
    rxn_keys: frozenset[Hashable]

    def __and__(self, other: "KeyOutput") -> "KeyOutput":
        new_mol_keys = self.mol_keys | other.mol_keys
        if len(new_mol_keys) > len(self.mol_keys) + len(other.mol_keys):
            raise KeyError(
                f"Conflicting molecule metadata key outputs {self.mol_keys & other.mol_keys}; separate expressions with >> or combine using other operator"
            )
        new_op_keys = self.op_keys | other.op_keys
        if len(new_op_keys) > len(self.op_keys) + len(other.op_keys):
            raise KeyError(
                f"Conflicting operator metadata key outputs {self.op_keys & other.op_keys}; separate expressions with >> or combine using other operator"
            )
        new_rxn_keys = self.rxn_keys | other.rxn_keys
        if len(new_rxn_keys) > len(self.rxn_keys) + len(other.rxn_keys):
            raise KeyError(
                f"Conflicting reaction metadata key outputs {self.rxn_keys & other.rxn_keys}; separate expressions with >> or combine using other operator"
            )
        return KeyOutput(new_mol_keys, new_op_keys, new_rxn_keys)


class PropertyCompositor(ABC):
    @abstractmethod
    def __call__(self, rxn: ReactionExplicit) -> "MetaPropertyState":
        ...

    @property
    @abstractmethod
    def keys(self) -> KeyOutput:
        ...

    @final
    def __and__(self, other: "PropertyCompositor") -> "PropertyCompositor":
        ...


@dataclass(frozen=True)
class MolPropertyCompositor(PropertyCompositor):
    __slots__ = ("_calc",)

    _calc: MolPropertyCalc

    def __call__(self, rxn: ReactionExplicit) -> "MetaPropertyState":
        mols = chain(rxn.reactants, rxn.products)
        props = {
            mol.item.uid: calc
            for mol, calc in ((mol, self._calc(mol)) for mol in mols)
            if calc is not None
        }
        single_state = MetaPropertyStateSingleProp(props, self._calc.resolver)
        return MetaPropertyState({self._calc.key: single_state}, {}, {})

    @property
    def keys(self) -> KeyOutput:
        return KeyOutput(frozenset((self._calc.key,)), frozenset(), frozenset())


@dataclass(frozen=True)
class MolRxnPropertyCompositor(PropertyCompositor):
    __slots__ = ("_calc",)

    _calc: MolPropertyFromRxnCalc

    def __call__(self, rxn: ReactionExplicit) -> "MetaPropertyState":
        mols = chain(rxn.reactants, rxn.products)
        props = {
            mol.item.uid: calc
            for mol, calc in ((mol, self._calc(mol, rxn)) for mol in mols)
            if calc is not None
        }
        single_state = MetaPropertyStateSingleProp(props, self._calc.resolver)
        return MetaPropertyState({self._calc.key: single_state}, {}, {})

    @property
    def keys(self) -> KeyOutput:
        return KeyOutput(frozenset((self._calc.key,)), frozenset(), frozenset())


@dataclass(frozen=True)
class OpPropertyCompositor(PropertyCompositor):
    __slots__ = ("_calc",)

    _calc: OpPropertyCalc

    def __call__(self, rxn: ReactionExplicit) -> "MetaPropertyState":
        calc = self._calc(rxn.operator)
        if calc is None:
            return MetaPropertyState({}, {}, {})
        props = {rxn.operator.item.uid: calc}
        single_state = MetaPropertyStateSingleProp(props, self._calc.resolver)
        return MetaPropertyState({}, {self._calc.key: single_state}, {})

    @property
    def keys(self) -> KeyOutput:
        return KeyOutput(frozenset(), frozenset((self._calc.key,)), frozenset())


@dataclass(frozen=True)
class OpRxnPropertyCompositor(PropertyCompositor):
    __slots__ = ("_calc",)

    _calc: OpPropertyFromRxnCalc

    def __call__(self, rxn: ReactionExplicit) -> "MetaPropertyState":
        calc = self._calc(rxn.operator, rxn)
        if calc is None:
            return MetaPropertyState({}, {}, {})
        props = {rxn.operator.item.uid: calc}
        single_state = MetaPropertyStateSingleProp(props, self._calc.resolver)
        return MetaPropertyState({}, {self._calc.key: single_state}, {})

    @property
    def keys(self) -> KeyOutput:
        return KeyOutput(frozenset(), frozenset((self._calc.key,)), frozenset())


@dataclass(frozen=True)
class RxnPropertyCompositor(PropertyCompositor):
    __slots__ = ("_calc",)

    _calc: MolPropertyCalc

    def __call__(self, rxn: ReactionExplicit) -> "MetaPropertyState":
        mols = chain(rxn.reactants, rxn.products)
        props = {
            mol.item.uid: calc
            for mol, calc in ((mol, self._calc(mol)) for mol in mols)
            if calc is not None
        }
        single_state = MetaPropertyStateSingleProp(props, self._calc.resolver)
        return MetaPropertyState({self._calc.key: single_state}, {}, {})

    @property
    def keys(self) -> KeyOutput:
        return KeyOutput(frozenset((self._calc.key,)), frozenset(), frozenset())


@dataclass
class MetaPropertyStateSingleProp(Generic[_T]):
    # note: this class is intended to be mutable; it will change after merging!
    __slots__ = ("data", "resolver")

    data: dict[Identifier, _T]
    resolver: MetaDataResolverFunc[_T]

    def __or__(
        self, other: "MetaPropertyStateSingleProp"[_T]
    ) -> "MetaPropertyStateSingleProp"[_T]:
        resolved_props: dict[Identifier, _T] = {}
        if len(self.data) == 0:
            return other
        if len(other.data) == 0:
            self.resolver = other.resolver
            return self
        common_keys = self.data.keys() & other.data.keys()
        if len(common_keys) == 0:
            other.data.update(self.data)
            return other
        for item_key in common_keys:
            resolved_props[item_key] = self.resolver(
                self.data[item_key], other.data[item_key]
            )
        other.data.update(self.data)
        other.data.update(resolved_props)
        return other


@dataclass
class MetaPropertyState:
    # note: this class is intended to be mutable; it will change after merging!
    __slots__ = ("mol_info", "op_info", "rxn_info")

    mol_info: dict[Hashable, MetaPropertyStateSingleProp]
    op_info: dict[Hashable, MetaPropertyStateSingleProp]
    rxn_info: dict[Hashable, MetaPropertyStateSingleProp]

    def __or__(self, other: "MetaPropertyState") -> "MetaPropertyState":
        if len(self.mol_info) == 0:
            self.mol_info = other.mol_info
        elif len(other.mol_info) != 0:
            common_keys = self.mol_info.keys() & other.mol_info.keys()
            if len(common_keys) == 0:
                self.mol_info.update(other.mol_info)
            else:
                resolved_info: dict[Hashable, Any] = {}
                for prop_key in common_keys:
                    resolved_info[prop_key] = (
                        self.mol_info[prop_key] | other.mol_info[prop_key]
                    )
                self.mol_info.update(other.mol_info)
                self.mol_info.update(resolved_info)
        if len(self.op_info) == 0:
            self.op_info = other.op_info
        elif len(other.op_info) != 0:
            common_keys = self.op_info.keys() & other.op_info.keys()
            if len(common_keys) == 0:
                self.op_info.update(other.op_info)
            else:
                resolved_info = {}
                for prop_key in common_keys:
                    resolved_info[prop_key] = (
                        self.op_info[prop_key] | other.op_info[prop_key]
                    )
                self.op_info.update(other.op_info)
                self.op_info.update(resolved_info)
        if len(self.rxn_info) == 0:
            self.rxn_info = other.rxn_info
        elif len(other.rxn_info) != 0:
            common_keys = self.rxn_info.keys() & other.rxn_info.keys()
            if len(common_keys) == 0:
                self.rxn_info.update(other.rxn_info)
            else:
                resolved_info = {}
                for prop_key in common_keys:
                    resolved_info[prop_key] = (
                        self.rxn_info[prop_key] | other.rxn_info[prop_key]
                    )
                self.rxn_info.update(other.rxn_info)
                self.rxn_info.update(resolved_info)

        return self


class RxnAnalysisStep(ABC):
    @abstractmethod
    def execute(
        self, rxns: Iterable[tuple[ReactionExplicit, bool]]
    ) -> Iterable[tuple[ReactionExplicit, bool]]:
        ...

    @final
    def __rshift__(self, other: "RxnAnalysisStep") -> "RxnAnalysisStepCompound":
        return RxnAnalysisStepCompound(self, other)


@dataclass(frozen=True)
class RxnAnalysisStepCompound(RxnAnalysisStep):
    __slots__ = ("step1", "step2")

    step1: RxnAnalysisStep
    step2: RxnAnalysisStep

    def execute(
        self, rxns: Iterable[tuple[ReactionExplicit, bool]]
    ) -> Iterable[tuple[ReactionExplicit, bool]]:
        return self.step2.execute(
            rxn_out for rxn_out in self.step1.execute(rxns)
        )


def SingleRxnAnalysisStep(
    arg: Union["PropertyCompositor", ReactionFilterBase]
) -> RxnAnalysisStep:
    if isinstance(arg, PropertyCompositor):
        return RxnAnalysisStepProp(arg)
    elif isinstance(arg, ReactionFilterBase):
        return RxnAnalysisStepFilter(arg)
    raise TypeError(
        f"Argument is of type {type(arg)}; must be LocalPropertyCalc or ReactionFilterBase"
    )


def _mmd(
    i1: Optional[Mapping[Hashable, Any]], i2: Optional[Mapping[Hashable, Any]]
) -> Optional[Mapping[Hashable, Any]]:
    if i1 is None:
        if i2 is None:
            return None
        return i2
    elif i2 is None:
        return i1
    if not isinstance(i1, dict):
        return dict(i1) | i2
    return i1 | i2


def metalib_to_rxn_meta(
    metalib: MetaPropertyState, rxns: Iterable[tuple[ReactionExplicit, bool]]
) -> Iterable[tuple[ReactionExplicit, bool]]:
    mol_info: dict[Identifier, dict[Hashable, Any]] = {}
    for meta_key, mol_dict in metalib.mol_info.items():
        for mol_id, key_val in mol_dict.data.items():
            mol_info[mol_id][meta_key] = key_val
    op_info: dict[Identifier, dict[Hashable, Any]] = {}
    for meta_key, op_dict in metalib.op_info.items():
        for op_id, key_val in op_dict.data.items():
            op_info[op_id][meta_key] = key_val
    rxn_info: dict[Identifier, dict[Hashable, Any]] = {}
    for meta_key, rxn_dict in metalib.rxn_info.items():
        for rxn_id, key_val in rxn_dict.data.items():
            rxn_info[rxn_id][meta_key] = key_val
    for rxn_all in rxns:
        rxn = rxn_all[0]
        op_data = DataPacketE(
            rxn.operator.i,
            rxn.operator.item,
            _mmd(rxn.operator.meta, op_info[rxn.operator.item.uid]),
        )
        reactant_data = tuple(
            DataPacketE(mol.i, mol.item, _mmd(mol.meta, mol_info[mol.item.uid]))
            for mol in rxn.reactants
        )
        product_data = tuple(
            DataPacketE(mol.i, mol.item, _mmd(mol.meta, mol_info[mol.item.uid]))
            for mol in rxn.products
        )
        yield (
            ReactionExplicit(
                op_data,
                reactant_data,
                product_data,
                _mmd(
                    rxn.reaction_meta,
                    rxn_info[
                        (
                            rxn.operator.item.uid,
                            tuple(mol.item.uid for mol in rxn.reactants),
                            tuple(mol.item.uid for mol in rxn.products),
                        )
                    ],
                ),
            ),
            rxn_all[1],
        )


@dataclass(frozen=True)
class RxnAnalysisStepProp(RxnAnalysisStep):
    __slots__ = ("_prop",)

    def __init__(self, arg: PropertyCompositor) -> None:
        self._prop = arg

    def execute(
        self, rxns: Iterable[tuple[ReactionExplicit, bool]]
    ) -> Iterable[tuple[ReactionExplicit, bool]]:
        rxn_list = list(rxns)
        meta_lib_generator = (self._prop(rxn[0]) for rxn in rxn_list if rxn[1])
        prop_map: MetaPropertyState = logreduce(
            operator.or_, meta_lib_generator
        )
        return metalib_to_rxn_meta(prop_map, rxn_list)


class RxnAnalysisStepFilter(RxnAnalysisStep):
    __slots__ = ("_arg",)

    def __init__(self, arg: ReactionFilterBase) -> None:
        self._arg = arg

    def execute(
        self, rxns: Iterable[tuple[ReactionExplicit, bool]]
    ) -> Iterable[tuple[ReactionExplicit, bool]]:
        for rxn, pass_filter in rxns:
            if not pass_filter or not self._arg(rxn):
                yield rxn, False
            else:
                yield rxn, True


class MetaUpdate(ABC):
    @abstractmethod
    def __call__(
        self, reaction: ReactionExplicit, network: ChemNetwork
    ) -> Optional[ReactionExplicit]:
        ...

    @final
    def __add__(self, other: "MetaUpdate") -> "MetaUpdate":
        return MetaUpdateMultiple((self, other))


@final
class MetaUpdateMultiple(MetaUpdate):
    __slots__ = "_composed_updates"
    _composed_updates: tuple[MetaUpdate, ...]

    def __init__(self, update_list: Collection[MetaUpdate]) -> None:
        updates: list[MetaUpdate] = []
        for update in update_list:
            if isinstance(update, MetaUpdateMultiple):
                updates.extend(update._composed_updates)
            else:
                updates.append(update)
        self._composed_updates = tuple(updates)

    def __call__(
        self, reaction: ReactionExplicit, network: ChemNetwork
    ) -> ReactionExplicit:
        cur_reaction = reaction
        for update in self._composed_updates:
            new_reaction = update(cur_reaction, network)
            if new_reaction is not None:
                cur_reaction = new_reaction
        return cur_reaction

#  Copyright 2023 Google LLC
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      https://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
from typing import Dict, Iterator, List, Tuple

import attr
import cirq
import numpy as np
import pytest
import sympy
from attrs import frozen

import qualtran
import qualtran.testing as qlt_testing
from qualtran import (
    Bloq,
    BloqBuilder,
    DecomposeNotImplementedError,
    GateWithRegisters,
    QAny,
    QBit,
    Register,
    Side,
    Signature,
    Soquet,
    SoquetT,
)
from qualtran._infra.gate_with_registers import get_named_qubits
from qualtran.bloqs.basic_gates import CNOT, OneState, YPowGate, ZeroState, ZPowGate
from qualtran.bloqs.bookkeeping import Allocate, Free, Join, Split
from qualtran.bloqs.mcmt.and_bloq import And
from qualtran.cirq_interop import cirq_optree_to_cbloq, CirqGateAsBloq, CirqQuregT
from qualtran.cirq_interop.t_complexity_protocol import TComplexity
from qualtran.resource_counting import GateCounts, get_cost_value, QECGatesCost


@frozen
class TestCNOT(Bloq):
    @property
    def signature(self) -> Signature:
        return Signature.build(control=1, target=1)

    def build_composite_bloq(self, bb: 'BloqBuilder', **soqs: 'SoquetT') -> Dict[str, 'SoquetT']:
        ctrl, target = soqs['control'], soqs['target']
        assert isinstance(ctrl, Soquet)
        assert isinstance(target, Soquet)
        ctrl, target = bb.add(CirqGateAsBloq(cirq.CNOT), q=[ctrl, target])
        return {'control': ctrl, 'target': target}

    def as_cirq_op(
        self, qubit_manager: cirq.QubitManager, **cirq_quregs: 'CirqQuregT'
    ) -> Tuple['cirq.Operation', Dict[str, 'CirqQuregT']]:
        (control,) = cirq_quregs['control']
        (target,) = cirq_quregs['target']
        return cirq.CNOT(control, target), cirq_quregs


def test_cirq_gate_as_bloq_for_trivial_gates():
    x = CirqGateAsBloq(cirq.X)
    rx = CirqGateAsBloq(cirq.Rx(rads=0.123 * np.pi))
    toffoli = CirqGateAsBloq(cirq.TOFFOLI)

    for b in [x, rx, toffoli]:
        assert len(b.signature) == 1
        assert b.signature[0].side == Side.THRU

    assert x.signature[0].shape == ()
    assert toffoli.signature[0].shape == (3,)

    assert str(x) == 'cirq.X'
    assert str(rx) == 'cirq.Rx(0.123π)'
    assert str(toffoli) == 'cirq.TOFFOLI'


def test_cirq_gate_as_bloq_tensor_contract_for_and_gate():
    and_gate = And()
    bb = BloqBuilder()
    ctrl = [bb.add(OneState()) for _ in range(2)]
    target = bb.add(ZeroState())
    q = [*ctrl, target]
    with pytest.warns(UserWarning):
        # It's odd to use CirqGateAsBloq to wrap a GateWithRegisters, which is already a bloq.
        c0, c1, target = bb.add(CirqGateAsBloq(and_gate), q=q)
    cbloq = bb.finalize(ctrl=np.array([c0, c1]), target=target)
    state_vector = cbloq.tensor_contract()
    assert np.isclose(state_vector[7], 1)

    with pytest.raises(NotImplementedError, match=r".*only supported for unitary gates.*"):
        _ = CirqGateAsBloq(And(uncompute=True)).as_composite_bloq().tensor_contract()


def test_bloq_decompose():
    tb = TestCNOT()
    assert len(tb.signature) == 2
    ctrl, trg = tb.signature
    assert ctrl.bitsize == 1
    assert ctrl.side == Side.THRU
    assert str(tb) == 'TestCNOT'

    cirq_quregs = get_named_qubits(tb.signature.lefts())
    circuit, _ = tb.decompose_bloq().to_cirq_circuit_and_quregs(**cirq_quregs, qubit_manager=None)
    assert circuit == cirq.Circuit(cirq.CNOT(*cirq_quregs['control'], *cirq_quregs['target']))
    assert tb.t_complexity() == TComplexity(clifford=1)


def test_cirq_circuit_to_cbloq():
    qubits = cirq.LineQubit.range(6)
    circuit = cirq.testing.random_circuit(qubits, n_moments=7, op_density=1.0, random_state=52)

    circuit.append(cirq.global_phase_operation(-1j))

    cbloq = cirq_optree_to_cbloq(circuit)

    bloq_unitary = cbloq.tensor_contract()
    cirq_unitary = circuit.unitary(qubits)
    np.testing.assert_allclose(cirq_unitary, bloq_unitary, atol=1e-8)


def test_cbloq_to_cirq_circuit():
    qubits = cirq.LineQubit.range(6)
    circuit = cirq.testing.random_circuit(qubits, n_moments=7, op_density=1.0, random_state=52)
    cbloq = cirq_optree_to_cbloq(circuit)

    # important! we lose moment structure
    circuit = cirq.Circuit(circuit.all_operations())

    # Note: a 1d `shape` bloq register is actually two-dimensional in cirq-world
    # because of the implicit `bitsize` dimension (which must be explicit in cirq-world).
    # CirqGate has registers of bitsize=1 and shape=(n,); hence the list transpose below.
    circuit2, _ = cbloq.to_cirq_circuit_and_quregs(
        qubits=np.asarray([[q] for q in qubits]), qubit_manager=cirq.ops.SimpleQubitManager()
    )

    assert circuit == circuit2


def test_cirq_optree_to_cbloq():
    @attr.frozen
    class CirqGateWithRegisters(GateWithRegisters):
        reg: Register

        @property
        def signature(self) -> Signature:
            return Signature([self.reg])

    reg1 = Register('x', QAny(2), shape=(3, 4))
    reg2 = Register('y', QAny(2), shape=12)
    anc_reg = Register('anc', QAny(2), shape=4)
    qubits = cirq.LineQubit.range(24)
    anc_qubits = cirq.NamedQubit.range(4, prefix='anc')
    circuit = cirq.Circuit(
        CirqGateWithRegisters(reg1).on(*qubits),
        CirqGateWithRegisters(anc_reg).on(*anc_qubits, *qubits[:4]),
        CirqGateWithRegisters(reg2).on(*qubits),
    )
    # Test-1: When no signature is specified, the method uses a default signature. Ancilla qubits
    # are also included in the signature itself, so no allocations / deallocations are needed.
    cbloq = cirq_optree_to_cbloq(circuit)
    assert cbloq.signature == qualtran.Signature([qualtran.Register('qubits', QBit(), shape=(28,))])
    bloq_instances = [binst for binst, _, _ in cbloq.iter_bloqnections()]
    # Greedy iteration of iter_bloqnections first joins only qubits needed
    # for the first gate.
    assert all(bloq_instances[i].bloq == Join(QAny(2)) for i in range(12))
    assert bloq_instances[12].bloq == CirqGateWithRegisters(reg1)
    assert bloq_instances[12].bloq.signature == qualtran.Signature(
        [qualtran.Register('x', QAny(bitsize=2), shape=(3, 4))]
    )
    assert bloq_instances[15].bloq == CirqGateWithRegisters(anc_reg)
    assert bloq_instances[15].bloq.signature == qualtran.Signature(
        [qualtran.Register('anc', QAny(bitsize=2), shape=(4,))]
    )
    assert bloq_instances[16].bloq == CirqGateWithRegisters(reg2)
    assert bloq_instances[16].bloq.signature == qualtran.Signature(
        [qualtran.Register('y', QAny(bitsize=2), shape=(12,))]
    )
    assert all(bloq_instances[-i].bloq == Split(QAny(2)) for i in range(1, 15))
    # Test-2: If you provide an explicit signature, you must also provide a mapping of cirq qubits
    # matching the signature. The additional ancilla allocations are automatically handled.
    new_signature = qualtran.Signature(
        [
            qualtran.Register('xx', QAny(bitsize=3), shape=(3, 2)),
            qualtran.Register('yy', QBit(), shape=(2, 3)),
        ]
    )
    cirq_quregs = {
        'xx': np.asarray(qubits[:18]).reshape((3, 2, 3)),
        'yy': np.asarray(qubits[18:]).reshape((2, 3, 1)),
    }
    cbloq = cirq_optree_to_cbloq(
        circuit, signature=new_signature, in_quregs=cirq_quregs, out_quregs=cirq_quregs
    )
    assert cbloq.signature == new_signature
    # Splits, joins, Alloc, Free are automatically inserted.
    bloqs_list = [binst.bloq for binst in cbloq.bloq_instances]
    assert bloqs_list.count(Split(QAny(3))) == 6
    assert bloqs_list.count(Join(QAny(3))) == 6
    assert bloqs_list.count(Allocate(QAny(2))) == 2
    assert bloqs_list.count(Free(QAny(2))) == 2


class LeftOnlyGate(GateWithRegisters):
    @property
    def signature(self):
        return Signature([Register('junk', QAny(2), side=Side.LEFT)])

    def decompose_from_registers(self, *, context, junk) -> Iterator[cirq.OP_TREE]:
        yield cirq.CNOT(*junk)
        yield cirq.reset_each(*junk)


def test_cirq_gate_as_bloq_for_left_only_gates():
    # `CirqGateAsBloq` only uses the `cirq.Gate` API,
    # so it has no concept of defining "left only" registers.
    bloq = CirqGateAsBloq(gate=LeftOnlyGate())
    cbloq = qlt_testing.assert_valid_bloq_decomposition(bloq)

    assert (
        cbloq.debug_text()
        == """\
CNOT<0>
  LeftDangle.q[0] -> ctrl
  LeftDangle.q[1] -> target
  ctrl -> cirq.reset<1>.q
  target -> cirq.reset<2>.q
--------------------
cirq.reset<1>
  CNOT<0>.ctrl -> q
  q -> RightDangle.q[0]
cirq.reset<2>
  CNOT<0>.target -> q
  q -> RightDangle.q[1]"""
    )


def test_gwr_for_left_only_gates():
    # This test is included in this file in contrast to `test_cirq_gate_as_bloq_for_left_only_gates`
    # See that test for the behavior of CirqGateAsBloq.
    class LeftOnlyGate(GateWithRegisters):
        @property
        def signature(self):
            return Signature([Register('junk', QAny(2), side=Side.LEFT)])

        def decompose_from_registers(self, *, context, junk) -> Iterator[cirq.OP_TREE]:
            yield cirq.CNOT(*junk)
            yield cirq.reset_each(*junk)

    # Using InteropQubitManager enables support for LeftOnlyGate's in CirqGateAsBloq.
    cbloq = qlt_testing.assert_valid_bloq_decomposition(LeftOnlyGate())
    bloqs_list: List[Bloq] = [binst.bloq for binst in cbloq.bloq_instances]
    assert bloqs_list.count(Split(QAny(2))) == 1
    assert bloqs_list.count(Free(QBit())) == 2
    assert bloqs_list.count(CNOT()) == 1
    assert bloqs_list.count(CirqGateAsBloq(cirq.ResetChannel())) == 2


def test_cirq_gate_as_bloq_decompose_raises():
    bloq = CirqGateAsBloq(cirq.X)
    with pytest.raises(DecomposeNotImplementedError, match="does not declare a decomposition"):
        _ = bloq.decompose_bloq()


def test_cirq_gate_cost_via_decomp():
    theta = sympy.Symbol("theta", real=True)
    cirq_swappow = cirq.SwapPowGate(exponent=theta)
    swappow_bloq = CirqGateAsBloq(cirq_swappow)

    _, sigma = swappow_bloq.call_graph()
    assert sigma == {
        CNOT(): 2,
        YPowGate(0.5): 1,
        YPowGate(-0.5): 1,
        And(): 1,
        And(uncompute=True): 1,
        ZPowGate(theta): 1,
    }

    gc_swappow = get_cost_value(swappow_bloq, QECGatesCost())
    assert gc_swappow == GateCounts(clifford=5, rotation=1, and_bloq=1, measurement=1)

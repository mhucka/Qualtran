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
import cirq
import numpy as np

from qualtran import BloqBuilder
from qualtran.bloqs.basic_gates import PlusState, SGate
from qualtran.bloqs.basic_gates.s_gate import _s_gate


def test_s_gate(bloq_autotester):
    bloq_autotester(_s_gate)


def test_call_graph():
    _, sigma = SGate().call_graph()
    assert sigma == {SGate(): 1}


def test_to_cirq():
    bb = BloqBuilder()
    q = bb.add(PlusState())
    q = bb.add(SGate(), q=q)
    q = bb.add(SGate().adjoint(), q=q)
    cbloq = bb.finalize(q=q)
    circuit = cbloq.to_cirq_circuit()
    cirq.testing.assert_has_diagram(circuit, "_c(0): ───H───S───S^-1───")


def test_pl_interop():
    import pennylane as qml

    bloq = SGate()
    pl_op_from_bloq = bloq.as_pl_op(wires=[0])
    pl_op = qml.S(wires=[0])
    assert pl_op_from_bloq == pl_op

    matrix = pl_op.matrix()
    should_be = bloq.tensor_contract()
    np.testing.assert_allclose(should_be, matrix)


def test_tensors():
    from_cirq = cirq.unitary(cirq.Circuit(cirq.S(cirq.LineQubit(0))))
    from_tensors = SGate().tensor_contract()
    np.testing.assert_allclose(from_cirq, from_tensors)

    from_cirq = cirq.unitary(cirq.Circuit(cirq.S(cirq.LineQubit(0)) ** -1))
    from_tensors = SGate().adjoint().tensor_contract()
    np.testing.assert_allclose(from_cirq, from_tensors)

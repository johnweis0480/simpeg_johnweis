from __future__ import print_function
import numpy as np
import unittest
from SimPEG import Mesh, Maps, Regularization, Utils, Tests, ObjectiveFunction
from scipy.sparse.linalg import dsolve
import inspect

TOL = 1e-10
testReg = True
testRegMesh = True

np.random.seed(639)

IGNORE_ME = [
    'BaseRegularization', 'BaseSparse', 'BaseSimpleSmooth',
    'BaseComboRegularization', 'BaseSmooth', 'BaseSmooth2'
]


class RegularizationTests(unittest.TestCase):

    def setUp(self):
        hx, hy, hz = np.random.rand(10), np.random.rand(9), np.random.rand(8)
        hx, hy, hz = hx/hx.sum(), hy/hy.sum(), hz/hz.sum()
        mesh1 = Mesh.TensorMesh([hx])
        mesh2 = Mesh.TensorMesh([hx, hy])
        mesh3 = Mesh.TensorMesh([hx, hy, hz])
        self.meshlist = [mesh1, mesh2, mesh3]

    if testReg:
        def test_regularization(self):
            for R in dir(Regularization):
                r = getattr(Regularization, R)
                if not inspect.isclass(r):
                    continue
                if not issubclass(r, ObjectiveFunction.BaseObjectiveFunction):
                    continue
                if r.__name__ in IGNORE_ME:
                    continue

                for i, mesh in enumerate(self.meshlist):

                    if mesh.dim < 3 and r.__name__[-1] == 'z':
                        continue
                    if mesh.dim < 2 and r.__name__[-1] == 'y':
                        continue

                    print('Testing {0:d}D'.format(mesh.dim))

                    mapping = Maps.IdentityMap(mesh)
                    reg = r(mesh=mesh, mapping=mapping)

                    print(
                        '--- Checking {} --- \n'.format(reg.__class__.__name__)
                    )

                    if mapping.nP != '*':
                        m = np.random.rand(mapping.nP)
                    else:
                        m = np.random.rand(mesh.nC)
                    mref = np.ones_like(m)*np.mean(m)
                    reg.mref = mref

                    print('Check: phi_m (mref) = {0:f}'.format(reg(mref)))
                    passed = reg(mref) < TOL
                    self.assertTrue(passed)

                    # test derivs
                    passed = reg.test(m)
                    self.assertTrue(passed)

        def test_regularization_ActiveCells(self):
            for R in dir(Regularization):
                r = getattr(Regularization, R)
                if not inspect.isclass(r):
                    continue
                if not issubclass(r, ObjectiveFunction.BaseObjectiveFunction):
                    continue
                if r.__name__ in IGNORE_ME:
                    continue

                for i, mesh in enumerate(self.meshlist):

                    print('Testing Active Cells {0:d}D'.format((mesh.dim)))

                    if mesh.dim == 1:
                        indActive = Utils.mkvc(mesh.gridCC <= 0.8)
                    elif mesh.dim == 2:
                        indActive = (
                            Utils.mkvc(mesh.gridCC[:, -1] <=
                                2*np.sin(2*np.pi*mesh.gridCC[:,0])+0.5)
                        )
                    elif mesh.dim == 3:
                        indActive = (
                            Utils.mkvc(mesh.gridCC[:, -1] <=
                            2 * np.sin(2*np.pi*mesh.gridCC[:,0])+0.5 *
                            2 * np.sin(2*np.pi*mesh.gridCC[:,1])+0.5)
                        )

                    if mesh.dim < 3 and r.__name__[-1] == 'z':
                        continue
                    if mesh.dim < 2 and r.__name__[-1] == 'y':
                        continue

                    for indAct in [indActive, indActive.nonzero()[0]]: # test both bool and integers
                        reg = r(mesh, indActive=indAct)
                        m = np.random.rand(mesh.nC)[indAct]
                        mref = np.ones_like(m)*np.mean(m)
                        reg.mref = mref

                        print(
                                '--- Checking {} ---\n'.format(
                                    reg.__class__.__name__
                                )
                            )
                        print(
                            'Check: phi_m (mref) = {0:f}'.format(reg(mref))
                        )
                        passed = reg(mref) < TOL
                        self.assertTrue(passed)

                        passed = reg.test(m)
                        self.assertTrue(passed)

    if testRegMesh:
        def test_regularizationMesh(self):

            for i, mesh in enumerate(self.meshlist):

                print('Testing {0:d}D'.format(mesh.dim))

                # mapping = r.mapPair(mesh)
                # reg = r(mesh, mapping=mapping)
                # m = np.random.rand(mapping.nP)

                if mesh.dim == 1:
                    indAct = Utils.mkvc(mesh.gridCC <= 0.8)
                elif mesh.dim == 2:
                    indAct = (
                        Utils.mkvc(
                            mesh.gridCC[:,-1] <=
                            2*np.sin(2*np.pi*mesh.gridCC[:, 0]) + 0.5
                        )
                    )
                elif mesh.dim == 3:
                    indAct = (
                        Utils.mkvc(
                            mesh.gridCC[:, -1] <=
                            2*np.sin(2*np.pi*mesh.gridCC[:, 0]) +
                            0.5 * 2*np.sin(2*np.pi*mesh.gridCC[:, 1]) + 0.5
                        )
                    )

                regmesh = Regularization.RegularizationMesh(
                    mesh, indActive=indAct
                )

                assert (regmesh.vol == mesh.vol[indAct]).all()

    def test_property_mirroring(self):
        mesh = Mesh.TensorMesh([8, 7, 6])

        for regType in ['Tikhonov', 'Sparse', 'Simple']:
            reg = getattr(Regularization, regType)(mesh)

            self.assertTrue(reg.nP == mesh.nC)

            # Test assignment of active indices
            indActive = mesh.gridCC[:, 2] < 0.6
            reg.indActive = indActive

            self.assertTrue(reg.nP == int(indActive.sum()))

            [
                self.assertTrue(np.all(fct.indActive == indActive))
                for fct in reg.objfcts
            ]

            # test assignment of cell weights
            cell_weights = np.random.rand(indActive.sum())
            reg.cell_weights = cell_weights
            [
                self.assertTrue(np.all(fct.cell_weights == cell_weights))
                for fct in reg.objfcts
            ]

            # test updated mappings
            mapping = Maps.ExpMap(nP=indActive.sum())
            reg.mapping = mapping
            m = np.random.rand(mapping.nP)
            [
                self.assertTrue(np.all(fct.mapping * m == mapping * m))
                for fct in reg.objfcts
            ]

            # test alphas
            m = np.random.rand(reg.nP)
            a = reg(m)
            [
                setattr(
                    reg, '{}'.format(objfct._multiplier_pair),
                    0.5*getattr(reg, '{}'.format(objfct._multiplier_pair))
                )
                for objfct in reg.objfcts
            ]
            b = reg(m)
            self.assertTrue(0.5*a == b)


if __name__ == '__main__':
    unittest.main()

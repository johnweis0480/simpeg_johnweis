import hashlib
import warnings
import numpy as np
from numpy.typing import NDArray
import scipy.sparse as sp
from geoana.kernels import (
    prism_fxxy,
    prism_fxxz,
    prism_fxyz,
    prism_fzx,
    prism_fzy,
    prism_fzz,
    prism_fzzz,
)
from scipy.constants import mu_0
from scipy.sparse.linalg import LinearOperator, aslinearoperator

from simpeg import props, utils
from simpeg.utils import mat_utils, mkvc, sdiag
from simpeg.utils.code_utils import deprecate_property, validate_string, validate_type
from simpeg.utils.solver_utils import get_default_solver

from ...base import BaseMagneticPDESimulation
from ..base import BaseEquivalentSourceLayerSimulation, BasePFSimulation
from .analytics import CongruousMagBC
from .survey import Survey

from ._numba import choclo, NUMBA_FUNCTIONS_3D, NUMBA_FUNCTIONS_2D

if choclo is not None:
    CHOCLO_SUPPORTED_COMPONENTS = {
        "tmi",
        "bx",
        "by",
        "bz",
        "bxx",
        "byy",
        "bzz",
        "bxy",
        "bxz",
        "byz",
        "tmi_x",
        "tmi_y",
        "tmi_z",
    }
    CHOCLO_KERNELS = {
        "bx": (choclo.prism.kernel_ee, choclo.prism.kernel_en, choclo.prism.kernel_eu),
        "by": (choclo.prism.kernel_en, choclo.prism.kernel_nn, choclo.prism.kernel_nu),
        "bz": (choclo.prism.kernel_eu, choclo.prism.kernel_nu, choclo.prism.kernel_uu),
        "bxx": (
            choclo.prism.kernel_eee,
            choclo.prism.kernel_een,
            choclo.prism.kernel_eeu,
        ),
        "byy": (
            choclo.prism.kernel_enn,
            choclo.prism.kernel_nnn,
            choclo.prism.kernel_nnu,
        ),
        "bzz": (
            choclo.prism.kernel_euu,
            choclo.prism.kernel_nuu,
            choclo.prism.kernel_uuu,
        ),
        "bxy": (
            choclo.prism.kernel_een,
            choclo.prism.kernel_enn,
            choclo.prism.kernel_enu,
        ),
        "bxz": (
            choclo.prism.kernel_eeu,
            choclo.prism.kernel_enu,
            choclo.prism.kernel_euu,
        ),
        "byz": (
            choclo.prism.kernel_enu,
            choclo.prism.kernel_nnu,
            choclo.prism.kernel_nuu,
        ),
        "tmi_x": (
            choclo.prism.kernel_eee,
            choclo.prism.kernel_enn,
            choclo.prism.kernel_euu,
            choclo.prism.kernel_een,
            choclo.prism.kernel_eeu,
            choclo.prism.kernel_enu,
        ),
        "tmi_y": (
            choclo.prism.kernel_een,
            choclo.prism.kernel_nnn,
            choclo.prism.kernel_nuu,
            choclo.prism.kernel_enn,
            choclo.prism.kernel_enu,
            choclo.prism.kernel_nnu,
        ),
        "tmi_z": (
            choclo.prism.kernel_eeu,
            choclo.prism.kernel_nnu,
            choclo.prism.kernel_uuu,
            choclo.prism.kernel_enu,
            choclo.prism.kernel_euu,
            choclo.prism.kernel_nuu,
        ),
    }
    CHOCLO_FORWARD_FUNCS = {
        "bx": choclo.prism.magnetic_e,
        "by": choclo.prism.magnetic_n,
        "bz": choclo.prism.magnetic_u,
        "bxx": choclo.prism.magnetic_ee,
        "byy": choclo.prism.magnetic_nn,
        "bzz": choclo.prism.magnetic_uu,
        "bxy": choclo.prism.magnetic_en,
        "bxz": choclo.prism.magnetic_eu,
        "byz": choclo.prism.magnetic_nu,
    }


class Simulation3DIntegral(BasePFSimulation):
    """
    Magnetic simulation in integral form.

    Parameters
    ----------
    mesh : discretize.TreeMesh or discretize.TensorMesh
        Mesh use to run the magnetic simulation.
    survey : simpeg.potential_fields.magnetics.Survey
        Magnetic survey with information of the receivers.
    active_cells : (n_cells) numpy.ndarray, optional
        Array that indicates which cells in ``mesh`` are active cells.
    chi : numpy.ndarray, optional
        Susceptibility array for the active cells in the mesh.
    chiMap : Mapping, optional
        Model mapping.
    model_type : str, optional
        Whether the model are susceptibilities of the cells (``"scalar"``),
        or effective susceptibilities (``"vector"``).
    is_amplitude_data : bool, optional
        If True, the returned fields will be the amplitude of the magnetic
        field. If False, the fields will be returned unmodified.
    sensitivity_dtype : numpy.dtype, optional
        Data type that will be used to build the sensitivity matrix.
    store_sensitivities : {"ram", "disk", "forward_only"}
        Options for storing sensitivity matrix. There are 3 options

        - 'ram': sensitivities are stored in the computer's RAM
        - 'disk': sensitivities are written to a directory
        - 'forward_only': you intend only do perform a forward simulation and
          sensitivities do not need to be stored

    sensitivity_path : str, optional
        Path to store the sensitivity matrix if ``store_sensitivities`` is set
        to ``"disk"``. Default to "./sensitivities".
    engine : {"geoana", "choclo"}, optional
       Choose which engine should be used to run the forward model.
    numba_parallel : bool, optional
        If True, the simulation will run in parallel. If False, it will
        run in serial. If ``engine`` is not ``"choclo"`` this argument will be
        ignored.
    ind_active : np.ndarray of int or bool

        .. deprecated:: 0.23.0

           Argument ``ind_active`` is deprecated in favor of
           ``active_cells`` and will be removed in SimPEG v0.24.0.
    """

    chi, chiMap, chiDeriv = props.Invertible("Magnetic Susceptibility (SI)")

    def __init__(
        self,
        mesh,
        chi=None,
        chiMap=None,
        model_type="scalar",
        is_amplitude_data=False,
        engine="geoana",
        numba_parallel=True,
        **kwargs,
    ):
        self.model_type = model_type
        super().__init__(mesh, engine=engine, numba_parallel=numba_parallel, **kwargs)
        self.chi = chi
        self.chiMap = chiMap

        self._M = None
        self.is_amplitude_data = is_amplitude_data
        self.modelMap = self.chiMap

        # Warn if n_processes has been passed
        if self.engine == "choclo" and "n_processes" in kwargs:
            warnings.warn(
                "The 'n_processes' will be ignored when selecting 'choclo' as the "
                "engine in the magnetic simulation.",
                UserWarning,
                stacklevel=1,
            )
            self.n_processes = None

    @property
    def model_type(self):
        """Type of magnetization model

        Returns
        -------
        str
            A string defining the model type for the simulation.
            One of {'scalar', 'vector'}.
        """
        return self._model_type

    @model_type.setter
    def model_type(self, value):
        self._model_type = validate_string("model_type", value, ["scalar", "vector"])

    @property
    def is_amplitude_data(self):
        return self._is_amplitude_data

    @is_amplitude_data.setter
    def is_amplitude_data(self, value):
        self._is_amplitude_data = validate_type("is_amplitude_data", value, bool)

    @property
    def M(self):
        """
        M: ndarray
            Magnetization matrix
        """
        if self.model_type == "vector":
            return None
        if getattr(self, "_M", None) is None:
            mag = self.survey.source_field.b0
            self._M = np.ones((self.nC, 3)) * mag
        return self._M

    @M.setter
    def M(self, M):
        """
        Create magnetization matrix from unit vector orientation
        :parameter
        M: array (3*nC,) or (nC, 3)
        """
        if self.model_type == "vector":
            self._M = sdiag(mkvc(M) * self.survey.source_field.amplitude)
        else:
            M = np.asarray(M)
            self._M = M.reshape((self.nC, 3))

    def fields(self, model):
        self.model = model
        # model = self.chiMap * model
        if self.store_sensitivities == "forward_only":
            if self.engine == "choclo":
                fields = self._forward(self.chi)
            else:
                fields = mkvc(self.linear_operator())
        else:
            fields = np.asarray(
                self.G @ self.chi.astype(self.sensitivity_dtype, copy=False)
            )

        if self.is_amplitude_data:
            fields = self.compute_amplitude(fields)

        return fields

    @property
    def G(self) -> NDArray | np.memmap | LinearOperator:
        if not hasattr(self, "_G"):
            match self.engine, self.store_sensitivities:
                case ("choclo", "forward_only"):
                    self._G = self._sensitivity_matrix_as_operator()
                case ("choclo", _):
                    self._G = self._sensitivity_matrix()
                case ("geoana", "forward_only"):
                    msg = (
                        "Accessing matrix G with "
                        'store_sensitivities="forward_only" and engine="geoana" '
                        "hasn't been implemented yet. "
                        'Choose store_sensitivities="ram" or "disk", '
                        'or another engine, like "choclo".'
                    )
                    raise NotImplementedError(msg)
                case ("geoana", _):
                    self._G = self.linear_operator()
        return self._G

    modelType = deprecate_property(
        model_type, "modelType", "model_type", removal_version="0.18.0", error=True
    )

    @property
    def nD(self):
        """
        Number of data
        """
        self._nD = self.survey.nD if not self.is_amplitude_data else self.survey.nD // 3
        return self._nD

    @property
    def tmi_projection(self):
        if getattr(self, "_tmi_projection", None) is None:
            # Convert from north to cartesian
            self._tmi_projection = mat_utils.dip_azimuth2cartesian(
                self.survey.source_field.inclination,
                self.survey.source_field.declination,
            ).squeeze()

        return self._tmi_projection

    def getJ(self, m, f=None) -> NDArray[np.float64 | np.float32] | LinearOperator:
        r"""
        Sensitivity matrix :math:`\mathbf{J}`.

        Parameters
        ----------
        m : (n_param,) numpy.ndarray
            The model parameters.
        f : Ignored
            Not used, present here for API consistency by convention.

        Returns
        -------
        (nD, n_params) np.ndarray or scipy.sparse.linalg.LinearOperator.
            Array or :class:`~scipy.sparse.linalg.LinearOperator` for the
            :math:`\mathbf{J}` matrix.
            A :class:`~scipy.sparse.linalg.LinearOperator` will be returned if
            ``store_sensitivities`` is ``"forward_only"``, otherwise a dense
            array will be returned.

        Notes
        -----
        If ``store_sensitivities`` is ``"ram"`` or ``"disk"``, a dense array
        for the ``J`` matrix is returned.
        A :class:`~scipy.sparse.linalg.LinearOperator` is returned if
        ``store_sensitivities`` is ``"forward_only"``. This object can perform
        operations like ``J @ m`` or ``J.T @ v`` without allocating the full
        ``J`` matrix in memory.
        """
        if self.is_amplitude_data:
            msg = (
                "The `getJ` method is not yet implemented to work with "
                "`is_amplitude_data`."
            )
            raise NotImplementedError(msg)

        # Need to assign the model, so the chiDeriv can be computed (if the
        # model is None, the chiDeriv is going to be Zero).
        self.model = m
        chiDeriv = (
            self.chiDeriv
            if not isinstance(self.G, LinearOperator)
            else aslinearoperator(self.chiDeriv)
        )
        return self.G @ chiDeriv

    def getJtJdiag(self, m, W=None, f=None):
        r"""
        Compute diagonal of :math:`\mathbf{J}^T \mathbf{J}``.

        Parameters
        ----------
        m : (n_param,) numpy.ndarray
            The model parameters.
        W : (nD, nD) np.ndarray or scipy.sparse.sparray, optional
            Diagonal matrix with the square root of the weights. If not None,
            the function returns the diagonal of
            :math:`\mathbf{J}^T \mathbf{W}^T \mathbf{W} \mathbf{J}``.
        f : Ignored
            Not used, present here for API consistency by convention.

        Returns
        -------
        (nparam) np.ndarray
            Array with the diagonal of ``J.T @ J``.

        Notes
        -----
        If ``store_sensitivities`` is ``"forward_only"``, the ``G`` matrix is
        never allocated in memory, and the diagonal is obtained by
        accumulation, computing each element of the ``G`` matrix on the fly.

        This method caches the diagonal ``G.T @ W.T @ W @ G`` and the sha256
        hash of the diagonal of the ``W`` matrix. This way, if same weights are
        passed to it, it reuses the cached diagonal so it doesn't need to be
        recomputed.
        If new weights are passed, the cache is updated with the latest
        diagonal of ``G.T @ W.T @ W @ G``.
        """
        # Need to assign the model, so the chiDeriv can be computed (if the
        # model is None, the chiDeriv is going to be Zero).
        self.model = m

        # We should probably check that W is diagonal. Let's assume it for now.
        weights = (
            W.diagonal() ** 2
            if W is not None
            else np.ones(self.survey.nD, dtype=np.float64)
        )

        # Compute gtg (G.T @ W.T @ W @ G) if it's not cached, or if the
        # weights are not the same.
        weights_sha256 = hashlib.sha256(weights)
        use_cached_gtg = (
            hasattr(self, "_gtg_diagonal")
            and hasattr(self, "_weights_sha256")
            and self._weights_sha256.digest() == weights_sha256.digest()
        )
        if not use_cached_gtg:
            self._gtg_diagonal = self._get_gtg_diagonal(weights)
            self._weights_sha256 = weights_sha256

        # Multiply the gtg_diagonal by the derivative of the mapping
        diagonal = mkvc(
            (sdiag(np.sqrt(self._gtg_diagonal)) @ self.chiDeriv).power(2).sum(axis=0)
        )
        return diagonal

    def _get_gtg_diagonal(self, weights: NDArray) -> NDArray:
        """
        Compute the diagonal of ``G.T @ W.T @ W @ G``.

        Parameters
        ----------
        weights : np.ndarray
            Weights array: diagonal of ``W.T @ W``.

        Returns
        -------
        np.ndarray
        """
        match (self.engine, self.store_sensitivities, self.is_amplitude_data):
            case ("geoana", "forward_only", _):
                msg = (
                    "Computing the diagonal of `G.T @ G` using "
                    "`'forward_only'` and `'geoana'` as engine hasn't been "
                    "implemented yet."
                )
                raise NotImplementedError(msg)
            case ("choclo", "forward_only", True):
                msg = (
                    "Computing the diagonal of `G.T @ G` using "
                    "`'forward_only'` and `is_amplitude_data` hasn't been "
                    "implemented yet."
                )
                raise NotImplementedError(msg)
            case ("choclo", "forward_only", False):
                gtg_diagonal = self._gtg_diagonal_without_building_g(weights)
            case (_, _, False):
                # In Einstein notation, the j-th element of the diagonal is:
                #   d_j = w_i * G_{ij} * G_{ij}
                gtg_diagonal = np.asarray(
                    np.einsum("i,ij,ij->j", weights, self.G, self.G)
                )
            case (_, _, True):
                ampDeriv = self.ampDeriv
                Gx = self.G[::3]
                Gy = self.G[1::3]
                Gz = self.G[2::3]
                gtg_diagonal = np.zeros(self.G.shape[1])
                for i in range(weights.size):
                    row = (
                        ampDeriv[0, i] * Gx[i]
                        + ampDeriv[1, i] * Gy[i]
                        + ampDeriv[2, i] * Gz[i]
                    )
                    gtg_diagonal += weights[i] * (row * row)
        return gtg_diagonal

    def Jvec(self, m, v, f=None):
        """
        Dot product between sensitivity matrix and a vector.

        Parameters
        ----------
        m : (n_param,) numpy.ndarray
            The model parameters. This array is used to compute the ``J``
            matrix.
        v : (n_param,) numpy.ndarray
            Vector used in the matrix-vector multiplication.
        f : Ignored
            Not used, present here for API consistency by convention.

        Returns
        -------
        (nD,) numpy.ndarray

        Notes
        -----
        If ``store_sensitivities`` is set to ``"forward_only"``, then the
        matrix `G` is never fully constructed, and the dot product is computed
        by accumulation, computing the matrix elements on the fly. Otherwise,
        the full matrix ``G`` is constructed and stored either in memory or
        disk.
        """
        # Need to assign the model, so the chiDeriv can be computed (if the
        # model is None, the chiDeriv is going to be Zero).
        self.model = m
        dmu_dm_v = self.chiDeriv @ v
        Jvec = self.G @ dmu_dm_v.astype(self.sensitivity_dtype, copy=False)

        if self.is_amplitude_data:
            # dask doesn't support an "order" argument to reshape...
            Jvec = Jvec.reshape((-1, 3)).T  # reshape((3, -1), order="F")
            ampDeriv_Jvec = self.ampDeriv * Jvec
            return ampDeriv_Jvec[0] + ampDeriv_Jvec[1] + ampDeriv_Jvec[2]

        return Jvec

    def Jtvec(self, m, v, f=None):
        """
        Dot product between transposed sensitivity matrix and a vector.

        Parameters
        ----------
        m : (n_param,) numpy.ndarray
            The model parameters. This array is used to compute the ``J``
            matrix.
        v : (nD,) numpy.ndarray
            Vector used in the matrix-vector multiplication.
        f : Ignored
            Not used, present here for API consistency by convention.

        Returns
        -------
        (nD,) numpy.ndarray

        Notes
        -----
        If ``store_sensitivities`` is set to ``"forward_only"``, then the
        matrix `G` is never fully constructed, and the dot product is computed
        by accumulation, computing the matrix elements on the fly. Otherwise,
        the full matrix ``G`` is constructed and stored either in memory or
        disk.
        """
        # Need to assign the model, so the chiDeriv can be computed (if the
        # model is None, the chiDeriv is going to be Zero).
        self.model = m

        if self.is_amplitude_data:
            v = self.ampDeriv * v
            # dask doesn't support and "order" argument to reshape...
            v = v.T.reshape(-1)  # .reshape(-1, order="F")
        Jtvec = self.G.T @ v.astype(self.sensitivity_dtype, copy=False)
        return np.asarray(self.chiDeriv.T @ Jtvec)

    @property
    def ampDeriv(self):
        if getattr(self, "_ampDeriv", None) is None:
            fields = np.asarray(
                self.G.dot(self.chi).astype(self.sensitivity_dtype, copy=False)
            )
            self._ampDeriv = self.normalized_fields(fields)

        return self._ampDeriv

    @classmethod
    def normalized_fields(cls, fields):
        """
        Return the normalized B fields
        """

        # Get field amplitude
        amp = cls.compute_amplitude(fields.astype(np.float64))

        return fields.reshape((3, -1), order="F") / amp[None, :]

    @classmethod
    def compute_amplitude(cls, b_xyz):
        """
        Compute amplitude of the magnetic field
        """

        amplitude = np.linalg.norm(b_xyz.reshape((3, -1), order="F"), axis=0)

        return amplitude

    def evaluate_integral(self, receiver_location, components):
        """
        Load in the active nodes of a tensor mesh and computes the magnetic
        forward relation between a cuboid and a given observation
        location outside the Earth [obsx, obsy, obsz]

        INPUT:
        receiver_location:  [obsx, obsy, obsz] nC x 3 Array

        components: list[str]
            List of magnetic components chosen from:
            'tmi', 'bx', 'by', 'bz', 'bxx', 'bxy', 'bxz', 'byy', 'byz', 'bzz', 'tmi_x', 'tmi_y', 'tmi_z'

        OUTPUT:
        Tx = [Txx Txy Txz]
        Ty = [Tyx Tyy Tyz]
        Tz = [Tzx Tzy Tzz]
        """
        dr = self._nodes - receiver_location
        dx = dr[..., 0]
        dy = dr[..., 1]
        dz = dr[..., 2]

        node_evals = {}
        if "bx" in components or "tmi" in components:
            node_evals["gxx"] = prism_fzz(dy, dz, dx)
            node_evals["gxy"] = prism_fzx(dy, dz, dx)
            node_evals["gxz"] = prism_fzy(dy, dz, dx)
        if "by" in components or "tmi" in components:
            if "gxy" not in node_evals:
                node_evals["gxy"] = prism_fzx(dy, dz, dx)
            node_evals["gyy"] = prism_fzz(dz, dx, dy)
            node_evals["gyz"] = prism_fzy(dx, dy, dz)
        if "bz" in components or "tmi" in components:
            if "gxz" not in node_evals:
                node_evals["gxz"] = prism_fzy(dy, dz, dx)
            if "gyz" not in node_evals:
                node_evals["gyz"] = prism_fzy(dx, dy, dz)
            node_evals["gzz"] = prism_fzz(dx, dy, dz)
            # the below will be uncommented when we give the containing cell index
            # for interior observations.
            # if "gxx" not in node_evals or "gyy" not in node_evals:
            #     node_evals["gzz"] = prism_fzz(dx, dy, dz)
            # else:
            #     # This is the one that would need to be adjusted if the observation is
            #     # inside an active cell.
            #     node_evals["gzz"] = -node_evals["gxx"] - node_evals["gyy"]

        if "bxx" in components or "tmi_x" in components:
            node_evals["gxxx"] = prism_fzzz(dy, dz, dx)
            node_evals["gxxy"] = prism_fxxy(dx, dy, dz)
            node_evals["gxxz"] = prism_fxxz(dx, dy, dz)
        if "bxy" in components or "tmi_x" in components or "tmi_y" in components:
            if "gxxy" not in node_evals:
                node_evals["gxxy"] = prism_fxxy(dx, dy, dz)
            node_evals["gyyx"] = prism_fxxz(dy, dz, dx)
            node_evals["gxyz"] = prism_fxyz(dx, dy, dz)
        if "bxz" in components or "tmi_x" in components or "tmi_z" in components:
            if "gxxz" not in node_evals:
                node_evals["gxxz"] = prism_fxxz(dx, dy, dz)
            if "gxyz" not in node_evals:
                node_evals["gxyz"] = prism_fxyz(dx, dy, dz)
            node_evals["gzzx"] = prism_fxxy(dz, dx, dy)
        if "byy" in components or "tmi_y" in components:
            if "gyyx" not in node_evals:
                node_evals["gyyx"] = prism_fxxz(dy, dz, dx)
            node_evals["gyyy"] = prism_fzzz(dz, dx, dy)
            node_evals["gyyz"] = prism_fxxy(dy, dz, dx)
        if "byz" in components or "tmi_y" in components or "tmi_z" in components:
            if "gxyz" not in node_evals:
                node_evals["gxyz"] = prism_fxyz(dx, dy, dz)
            if "gyyz" not in node_evals:
                node_evals["gyyz"] = prism_fxxy(dy, dz, dx)
            node_evals["gzzy"] = prism_fxxz(dz, dx, dy)
        if "bzz" in components or "tmi_z" in components:
            if "gzzx" not in node_evals:
                node_evals["gzzx"] = prism_fxxy(dz, dx, dy)
            if "gzzy" not in node_evals:
                node_evals["gzzy"] = prism_fxxz(dz, dx, dy)
            node_evals["gzzz"] = prism_fzzz(dx, dy, dz)

        ## Hxx = gxxx * m_x + gxxy * m_y + gxxz * m_z
        ## Hxy = gxxy * m_x + gyyx * m_y + gxyz * m_z
        ## Hxz = gxxz * m_x + gxyz * m_y + gzzx * m_z
        ## Hyy = gyyx * m_x + gyyy * m_y + gyyz * m_z
        ## Hyz = gxyz * m_x + gyyz * m_y + gzzy * m_z
        ## Hzz = gzzx * m_x + gzzy * m_y + gzzz * m_z

        rows = {}
        M = self.M
        for component in set(components):
            if component == "bx":
                vals_x = node_evals["gxx"]
                vals_y = node_evals["gxy"]
                vals_z = node_evals["gxz"]
            elif component == "by":
                vals_x = node_evals["gxy"]
                vals_y = node_evals["gyy"]
                vals_z = node_evals["gyz"]
            elif component == "bz":
                vals_x = node_evals["gxz"]
                vals_y = node_evals["gyz"]
                vals_z = node_evals["gzz"]
            elif component == "tmi":
                tmi = self.tmi_projection
                vals_x = (
                    tmi[0] * node_evals["gxx"]
                    + tmi[1] * node_evals["gxy"]
                    + tmi[2] * node_evals["gxz"]
                )
                vals_y = (
                    tmi[0] * node_evals["gxy"]
                    + tmi[1] * node_evals["gyy"]
                    + tmi[2] * node_evals["gyz"]
                )
                vals_z = (
                    tmi[0] * node_evals["gxz"]
                    + tmi[1] * node_evals["gyz"]
                    + tmi[2] * node_evals["gzz"]
                )
            elif component == "tmi_x":
                tmi = self.tmi_projection
                vals_x = (
                    tmi[0] * node_evals["gxxx"]
                    + tmi[1] * node_evals["gxxy"]
                    + tmi[2] * node_evals["gxxz"]
                )
                vals_y = (
                    tmi[0] * node_evals["gxxy"]
                    + tmi[1] * node_evals["gyyx"]
                    + tmi[2] * node_evals["gxyz"]
                )
                vals_z = (
                    tmi[0] * node_evals["gxxz"]
                    + tmi[1] * node_evals["gxyz"]
                    + tmi[2] * node_evals["gzzx"]
                )
            elif component == "tmi_y":
                tmi = self.tmi_projection
                vals_x = (
                    tmi[0] * node_evals["gxxy"]
                    + tmi[1] * node_evals["gyyx"]
                    + tmi[2] * node_evals["gxyz"]
                )
                vals_y = (
                    tmi[0] * node_evals["gyyx"]
                    + tmi[1] * node_evals["gyyy"]
                    + tmi[2] * node_evals["gyyz"]
                )
                vals_z = (
                    tmi[0] * node_evals["gxyz"]
                    + tmi[1] * node_evals["gyyz"]
                    + tmi[2] * node_evals["gzzy"]
                )
            elif component == "tmi_z":
                tmi = self.tmi_projection
                vals_x = (
                    tmi[0] * node_evals["gxxz"]
                    + tmi[1] * node_evals["gxyz"]
                    + tmi[2] * node_evals["gzzx"]
                )
                vals_y = (
                    tmi[0] * node_evals["gxyz"]
                    + tmi[1] * node_evals["gyyz"]
                    + tmi[2] * node_evals["gzzy"]
                )
                vals_z = (
                    tmi[0] * node_evals["gzzx"]
                    + tmi[1] * node_evals["gzzy"]
                    + tmi[2] * node_evals["gzzz"]
                )
            elif component == "bxx":
                vals_x = node_evals["gxxx"]
                vals_y = node_evals["gxxy"]
                vals_z = node_evals["gxxz"]
            elif component == "bxy":
                vals_x = node_evals["gxxy"]
                vals_y = node_evals["gyyx"]
                vals_z = node_evals["gxyz"]
            elif component == "bxz":
                vals_x = node_evals["gxxz"]
                vals_y = node_evals["gxyz"]
                vals_z = node_evals["gzzx"]
            elif component == "byy":
                vals_x = node_evals["gyyx"]
                vals_y = node_evals["gyyy"]
                vals_z = node_evals["gyyz"]
            elif component == "byz":
                vals_x = node_evals["gxyz"]
                vals_y = node_evals["gyyz"]
                vals_z = node_evals["gzzy"]
            elif component == "bzz":
                vals_x = node_evals["gzzx"]
                vals_y = node_evals["gzzy"]
                vals_z = node_evals["gzzz"]
            if self._unique_inv is not None:
                vals_x = vals_x[self._unique_inv]
                vals_y = vals_y[self._unique_inv]
                vals_z = vals_z[self._unique_inv]

            cell_eval_x = (
                vals_x[0]
                - vals_x[1]
                - vals_x[2]
                + vals_x[3]
                - vals_x[4]
                + vals_x[5]
                + vals_x[6]
                - vals_x[7]
            )
            cell_eval_y = (
                vals_y[0]
                - vals_y[1]
                - vals_y[2]
                + vals_y[3]
                - vals_y[4]
                + vals_y[5]
                + vals_y[6]
                - vals_y[7]
            )
            cell_eval_z = (
                vals_z[0]
                - vals_z[1]
                - vals_z[2]
                + vals_z[3]
                - vals_z[4]
                + vals_z[5]
                + vals_z[6]
                - vals_z[7]
            )
            if self.model_type == "vector":
                cell_vals = (
                    np.r_[cell_eval_x, cell_eval_y, cell_eval_z]
                ) * self.survey.source_field.amplitude
            else:
                cell_vals = (
                    cell_eval_x * M[:, 0]
                    + cell_eval_y * M[:, 1]
                    + cell_eval_z * M[:, 2]
                )

            if self.store_sensitivities == "forward_only":
                rows[component] = cell_vals @ self.chi
            else:
                rows[component] = cell_vals

            rows[component] /= 4 * np.pi

        return np.stack(
            [
                rows[component].astype(self.sensitivity_dtype, copy=False)
                for component in components
            ]
        )

    @property
    def _delete_on_model_update(self):
        deletes = super()._delete_on_model_update
        if self.is_amplitude_data:
            deletes = deletes + ["_gtg_diagonal", "_ampDeriv"]
        return deletes

    def _forward(self, model):
        """
        Forward model the fields of active cells in the mesh on receivers.

        Parameters
        ----------
        model : (n_active_cells) or (3 * n_active_cells) array
            Array containing the susceptibilities (scalar) or effective
            susceptibilities (vector) of the active cells in the mesh, in SI
            units.
            Susceptibilities are expected if ``model_type`` is ``"scalar"``,
            and the array should have ``n_active_cells`` elements.
            Effective susceptibilities are expected if ``model_type`` is
            ``"vector"``, and the array should have ``3 * n_active_cells``
            elements.

        Returns
        -------
        (nD, ) array
            Always return a ``np.float64`` array.
        """
        # Gather active nodes and the indices of the nodes for each active cell
        active_nodes, active_cell_nodes = self._get_active_nodes()
        # Get regional field
        regional_field = self.survey.source_field.b0
        # Allocate fields array
        fields = np.zeros(self.survey.nD, dtype=self.sensitivity_dtype)
        # Define the constant factor
        constant_factor = 1 / 4 / np.pi
        # Start computing the fields
        index_offset = 0
        scalar_model = self.model_type == "scalar"
        for components, receivers in self._get_components_and_receivers():
            if not CHOCLO_SUPPORTED_COMPONENTS.issuperset(components):
                raise NotImplementedError(
                    f"Other components besides {CHOCLO_SUPPORTED_COMPONENTS} "
                    "aren't implemented yet."
                )
            n_components = len(components)
            n_rows = n_components * receivers.shape[0]
            for i, component in enumerate(components):
                vector_slice = slice(
                    index_offset + i, index_offset + n_rows, n_components
                )
                if component == "tmi":
                    forward_func = NUMBA_FUNCTIONS_3D["forward"]["tmi"][
                        self.numba_parallel
                    ]
                    forward_func(
                        receivers,
                        active_nodes,
                        model,
                        fields[vector_slice],
                        active_cell_nodes,
                        regional_field,
                        constant_factor,
                        scalar_model,
                    )
                elif component in ("tmi_x", "tmi_y", "tmi_z"):
                    kernel_xx, kernel_yy, kernel_zz, kernel_xy, kernel_xz, kernel_yz = (
                        CHOCLO_KERNELS[component]
                    )
                    forward_func = NUMBA_FUNCTIONS_3D["forward"]["tmi_derivative"][
                        self.numba_parallel
                    ]
                    forward_func(
                        receivers,
                        active_nodes,
                        model,
                        fields[vector_slice],
                        active_cell_nodes,
                        regional_field,
                        kernel_xx,
                        kernel_yy,
                        kernel_zz,
                        kernel_xy,
                        kernel_xz,
                        kernel_yz,
                        constant_factor,
                        scalar_model,
                    )
                else:
                    kernel_x, kernel_y, kernel_z = CHOCLO_KERNELS[component]
                    forward_func = NUMBA_FUNCTIONS_3D["forward"]["magnetic_component"][
                        self.numba_parallel
                    ]
                    forward_func(
                        receivers,
                        active_nodes,
                        model,
                        fields[vector_slice],
                        active_cell_nodes,
                        regional_field,
                        kernel_x,
                        kernel_y,
                        kernel_z,
                        constant_factor,
                        scalar_model,
                    )
            index_offset += n_rows
        return fields

    def _sensitivity_matrix(self):
        """
        Compute the sensitivity matrix G

        Returns
        -------
        (nD, n_active_cells) array
        """
        # Gather active nodes and the indices of the nodes for each active cell
        active_nodes, active_cell_nodes = self._get_active_nodes()
        # Get regional field
        regional_field = self.survey.source_field.b0
        # Allocate sensitivity matrix
        scalar_model = self.model_type == "scalar"
        n_columns = self.nC if scalar_model else 3 * self.nC
        shape = (self.survey.nD, n_columns)
        if self.store_sensitivities == "disk":
            sensitivity_matrix = np.memmap(
                self.sensitivity_path,
                shape=shape,
                dtype=self.sensitivity_dtype,
                order="C",  # it's more efficient to write in row major
                mode="w+",
            )
        else:
            sensitivity_matrix = np.empty(shape, dtype=self.sensitivity_dtype)
        # Define the constant factor
        constant_factor = 1 / 4 / np.pi
        # Start filling the sensitivity matrix
        index_offset = 0
        for components, receivers in self._get_components_and_receivers():
            if not CHOCLO_SUPPORTED_COMPONENTS.issuperset(components):
                raise NotImplementedError(
                    f"Other components besides {CHOCLO_SUPPORTED_COMPONENTS} "
                    "aren't implemented yet."
                )
            n_components = len(components)
            n_rows = n_components * receivers.shape[0]
            for i, component in enumerate(components):
                matrix_slice = slice(
                    index_offset + i, index_offset + n_rows, n_components
                )
                if component == "tmi":
                    sensitivity_func = NUMBA_FUNCTIONS_3D["sensitivity"]["tmi"][
                        self.numba_parallel
                    ]
                    sensitivity_func(
                        receivers,
                        active_nodes,
                        sensitivity_matrix[matrix_slice, :],
                        active_cell_nodes,
                        regional_field,
                        constant_factor,
                        scalar_model,
                    )
                elif component in ("tmi_x", "tmi_y", "tmi_z"):
                    sensitivity_func = NUMBA_FUNCTIONS_3D["sensitivity"][
                        "tmi_derivative"
                    ][self.numba_parallel]
                    kernel_xx, kernel_yy, kernel_zz, kernel_xy, kernel_xz, kernel_yz = (
                        CHOCLO_KERNELS[component]
                    )
                    sensitivity_func(
                        receivers,
                        active_nodes,
                        sensitivity_matrix[matrix_slice, :],
                        active_cell_nodes,
                        regional_field,
                        kernel_xx,
                        kernel_yy,
                        kernel_zz,
                        kernel_xy,
                        kernel_xz,
                        kernel_yz,
                        constant_factor,
                        scalar_model,
                    )
                else:
                    sensitivity_func = NUMBA_FUNCTIONS_3D["sensitivity"][
                        "magnetic_component"
                    ][self.numba_parallel]
                    kernel_x, kernel_y, kernel_z = CHOCLO_KERNELS[component]
                    sensitivity_func(
                        receivers,
                        active_nodes,
                        sensitivity_matrix[matrix_slice, :],
                        active_cell_nodes,
                        regional_field,
                        kernel_x,
                        kernel_y,
                        kernel_z,
                        constant_factor,
                        scalar_model,
                    )
            index_offset += n_rows
        return sensitivity_matrix

    def _sensitivity_matrix_as_operator(self):
        """
        Create a LinearOperator for the sensitivity matrix G.

        Returns
        -------
        scipy.sparse.linalg.LinearOperator
        """
        n_columns = self.nC if self.model_type == "scalar" else self.nC * 3
        shape = (self.survey.nD, n_columns)
        linear_op = LinearOperator(
            shape=shape,
            matvec=self._forward,
            rmatvec=self._sensitivity_matrix_transpose_dot_vec,
            dtype=np.float64,
        )
        return linear_op

    def _sensitivity_matrix_transpose_dot_vec(self, vector):
        """
        Compute ``G.T @ v`` without building ``G``.

        Parameters
        ----------
        vector : (nD) numpy.ndarray
            Vector used in the dot product.

        Returns
        -------
        (n_active_cells) or (3 * n_active_cells) numpy.ndarray
        """
        # Gather active nodes and the indices of the nodes for each active cell
        active_nodes, active_cell_nodes = self._get_active_nodes()
        # Get regional field
        regional_field = self.survey.source_field.b0

        # Allocate resulting array.
        scalar_model = self.model_type == "scalar"
        result = np.zeros(self.nC if scalar_model else 3 * self.nC)

        # Define the constant factor
        constant_factor = 1 / 4 / np.pi

        # Fill the result array
        index_offset = 0
        for components, receivers in self._get_components_and_receivers():
            if not CHOCLO_SUPPORTED_COMPONENTS.issuperset(components):
                raise NotImplementedError(
                    f"Other components besides {CHOCLO_SUPPORTED_COMPONENTS} "
                    "aren't implemented yet."
                )
            n_components = len(components)
            n_rows = n_components * receivers.shape[0]
            for i, component in enumerate(components):
                vector_slice = slice(
                    index_offset + i, index_offset + n_rows, n_components
                )
                if component == "tmi":
                    gt_dot_v_func = NUMBA_FUNCTIONS_3D["gt_dot_v"]["tmi"][
                        self.numba_parallel
                    ]
                    gt_dot_v_func(
                        receivers,
                        active_nodes,
                        active_cell_nodes,
                        regional_field,
                        constant_factor,
                        scalar_model,
                        vector[vector_slice],
                        result,
                    )
                elif component in ("tmi_x", "tmi_y", "tmi_z"):
                    kernel_xx, kernel_yy, kernel_zz, kernel_xy, kernel_xz, kernel_yz = (
                        CHOCLO_KERNELS[component]
                    )
                    gt_dot_v_func = NUMBA_FUNCTIONS_3D["gt_dot_v"]["tmi_derivative"][
                        self.numba_parallel
                    ]
                    gt_dot_v_func(
                        receivers,
                        active_nodes,
                        active_cell_nodes,
                        regional_field,
                        kernel_xx,
                        kernel_yy,
                        kernel_zz,
                        kernel_xy,
                        kernel_xz,
                        kernel_yz,
                        constant_factor,
                        scalar_model,
                        vector[vector_slice],
                        result,
                    )
                else:
                    kernel_x, kernel_y, kernel_z = CHOCLO_KERNELS[component]
                    gt_dot_v_func = NUMBA_FUNCTIONS_3D["gt_dot_v"][
                        "magnetic_component"
                    ][self.numba_parallel]
                    gt_dot_v_func(
                        receivers,
                        active_nodes,
                        active_cell_nodes,
                        regional_field,
                        kernel_x,
                        kernel_y,
                        kernel_z,
                        constant_factor,
                        scalar_model,
                        vector[vector_slice],
                        result,
                    )
            index_offset += n_rows
        return result

    def _gtg_diagonal_without_building_g(self, weights):
        """
        Compute the diagonal of ``G.T @ G`` without building the ``G`` matrix.

        Parameters
        -----------
        weights : (nD,) array
            Array with data weights. It should be the diagonal of the ``W``
            matrix, squared.

        Returns
        -------
        (n_active_cells) numpy.ndarray
        """
        # Gather active nodes and the indices of the nodes for each active cell
        active_nodes, active_cell_nodes = self._get_active_nodes()
        # Get regional field
        regional_field = self.survey.source_field.b0
        # Define the constant factor
        constant_factor = 1 / 4 / np.pi

        # Allocate array for the diagonal
        scalar_model = self.model_type == "scalar"
        n_columns = self.nC if scalar_model else 3 * self.nC
        diagonal = np.zeros(n_columns, dtype=np.float64)

        # Start filling the diagonal array
        for components, receivers in self._get_components_and_receivers():
            if not CHOCLO_SUPPORTED_COMPONENTS.issuperset(components):
                raise NotImplementedError(
                    f"Other components besides {CHOCLO_SUPPORTED_COMPONENTS} "
                    "aren't implemented yet."
                )
            for component in components:
                if component == "tmi":
                    diagonal_gtg_func = NUMBA_FUNCTIONS_3D["diagonal_gtg"]["tmi"][
                        self.numba_parallel
                    ]
                    diagonal_gtg_func(
                        receivers,
                        active_nodes,
                        active_cell_nodes,
                        regional_field,
                        constant_factor,
                        scalar_model,
                        weights,
                        diagonal,
                    )
                elif component in ("tmi_x", "tmi_y", "tmi_z"):
                    kernel_xx, kernel_yy, kernel_zz, kernel_xy, kernel_xz, kernel_yz = (
                        CHOCLO_KERNELS[component]
                    )
                    diagonal_gtg_func = NUMBA_FUNCTIONS_3D["diagonal_gtg"][
                        "tmi_derivative"
                    ][self.numba_parallel]
                    diagonal_gtg_func(
                        receivers,
                        active_nodes,
                        active_cell_nodes,
                        regional_field,
                        kernel_xx,
                        kernel_yy,
                        kernel_zz,
                        kernel_xy,
                        kernel_xz,
                        kernel_yz,
                        constant_factor,
                        scalar_model,
                        weights,
                        diagonal,
                    )
                else:
                    kernel_x, kernel_y, kernel_z = CHOCLO_KERNELS[component]
                    diagonal_gtg_func = NUMBA_FUNCTIONS_3D["diagonal_gtg"][
                        "magnetic_component"
                    ][self.numba_parallel]
                    diagonal_gtg_func(
                        receivers,
                        active_nodes,
                        active_cell_nodes,
                        regional_field,
                        kernel_x,
                        kernel_y,
                        kernel_z,
                        constant_factor,
                        scalar_model,
                        weights,
                        diagonal,
                    )
        return diagonal


class SimulationEquivalentSourceLayer(
    BaseEquivalentSourceLayerSimulation, Simulation3DIntegral
):
    """
    Equivalent source layer simulation

    Parameters
    ----------
    mesh : discretize.BaseMesh
        A 2D tensor or tree mesh defining discretization along the x and y directions
    cell_z_top : numpy.ndarray or float
        Define the elevations for the top face of all cells in the layer.
        If an array it should be the same size as the active cell set.
    cell_z_bottom : numpy.ndarray or float
        Define the elevations for the bottom face of all cells in the layer.
        If an array it should be the same size as the active cell set.
    engine : {"geoana", "choclo"}, optional
        Choose which engine should be used to run the forward model.
    numba_parallel : bool, optional
        If True, the simulation will run in parallel. If False, it will
        run in serial. If ``engine`` is not ``"choclo"`` this argument will be
        ignored.

    """

    def __init__(
        self,
        mesh,
        cell_z_top,
        cell_z_bottom,
        engine="geoana",
        numba_parallel=True,
        **kwargs,
    ):
        super().__init__(
            mesh,
            cell_z_top,
            cell_z_bottom,
            engine=engine,
            numba_parallel=numba_parallel,
            **kwargs,
        )

    def _forward(self, model):
        """
        Forward model the fields of active cells in the mesh on receivers.

        Parameters
        ----------
        model : (n_active_cells) or (3 * n_active_cells) array
            Array containing the susceptibilities (scalar) or effective
            susceptibilities (vector) of the active cells in the mesh, in SI
            units.
            Susceptibilities are expected if ``model_type`` is ``"scalar"``,
            and the array should have ``n_active_cells`` elements.
            Effective susceptibilities are expected if ``model_type`` is
            ``"vector"``, and the array should have ``3 * n_active_cells``
            elements.

        Returns
        -------
        (nD, ) array
            Always return a ``np.float64`` array.
        """
        # Get cells in the 2D mesh and keep only active cells
        cells_bounds_active = self.mesh.cell_bounds[self.active_cells]
        # Get regional field
        regional_field = self.survey.source_field.b0
        # Allocate fields array
        fields = np.zeros(self.survey.nD, dtype=self.sensitivity_dtype)
        # Start computing the fields
        index_offset = 0
        scalar_model = self.model_type == "scalar"
        for components, receivers in self._get_components_and_receivers():
            if not CHOCLO_SUPPORTED_COMPONENTS.issuperset(components):
                raise NotImplementedError(
                    f"Other components besides {CHOCLO_SUPPORTED_COMPONENTS} "
                    "aren't implemented yet."
                )
            n_components = len(components)
            n_rows = n_components * receivers.shape[0]
            for i, component in enumerate(components):
                vector_slice = slice(
                    index_offset + i, index_offset + n_rows, n_components
                )
                if component == "tmi":
                    forward_func = NUMBA_FUNCTIONS_2D["forward"]["tmi"][
                        self.numba_parallel
                    ]
                    forward_func(
                        receivers,
                        cells_bounds_active,
                        self.cell_z_top,
                        self.cell_z_bottom,
                        model,
                        fields[vector_slice],
                        regional_field,
                        scalar_model,
                    )
                elif component in ("tmi_x", "tmi_y", "tmi_z"):
                    kernel_xx, kernel_yy, kernel_zz, kernel_xy, kernel_xz, kernel_yz = (
                        CHOCLO_KERNELS[component]
                    )
                    forward_func = NUMBA_FUNCTIONS_2D["forward"]["tmi_derivative"][
                        self.numba_parallel
                    ]
                    forward_func(
                        receivers,
                        cells_bounds_active,
                        self.cell_z_top,
                        self.cell_z_bottom,
                        model,
                        fields[vector_slice],
                        regional_field,
                        kernel_xx,
                        kernel_yy,
                        kernel_zz,
                        kernel_xy,
                        kernel_xz,
                        kernel_yz,
                        scalar_model,
                    )
                else:
                    choclo_forward_func = CHOCLO_FORWARD_FUNCS[component]
                    forward_func = NUMBA_FUNCTIONS_2D["forward"]["magnetic_component"][
                        self.numba_parallel
                    ]
                    forward_func(
                        receivers,
                        cells_bounds_active,
                        self.cell_z_top,
                        self.cell_z_bottom,
                        model,
                        fields[vector_slice],
                        regional_field,
                        choclo_forward_func,
                        scalar_model,
                    )
            index_offset += n_rows
        return fields

    def _sensitivity_matrix(self):
        """
        Compute the sensitivity matrix G

        Returns
        -------
        (nD, n_active_cells) array
        """
        # Get cells in the 2D mesh and keep only active cells
        cells_bounds_active = self.mesh.cell_bounds[self.active_cells]
        # Get regional field
        regional_field = self.survey.source_field.b0
        # Allocate sensitivity matrix
        scalar_model = self.model_type == "scalar"
        n_columns = self.nC if scalar_model else 3 * self.nC
        shape = (self.survey.nD, n_columns)
        if self.store_sensitivities == "disk":
            sensitivity_matrix = np.memmap(
                self.sensitivity_path,
                shape=shape,
                dtype=self.sensitivity_dtype,
                order="C",  # it's more efficient to write in row major
                mode="w+",
            )
        else:
            sensitivity_matrix = np.empty(shape, dtype=self.sensitivity_dtype)
        # Start filling the sensitivity matrix
        index_offset = 0
        for components, receivers in self._get_components_and_receivers():
            if not CHOCLO_SUPPORTED_COMPONENTS.issuperset(components):
                raise NotImplementedError(
                    f"Other components besides {CHOCLO_SUPPORTED_COMPONENTS} "
                    "aren't implemented yet."
                )
            n_components = len(components)
            n_rows = n_components * receivers.shape[0]
            for i, component in enumerate(components):
                matrix_slice = slice(
                    index_offset + i, index_offset + n_rows, n_components
                )
                if component == "tmi":
                    sensitivity_func = NUMBA_FUNCTIONS_2D["sensitivity"]["tmi"][
                        self.numba_parallel
                    ]
                    sensitivity_func(
                        receivers,
                        cells_bounds_active,
                        self.cell_z_top,
                        self.cell_z_bottom,
                        sensitivity_matrix[matrix_slice, :],
                        regional_field,
                        scalar_model,
                    )
                elif component in ("tmi_x", "tmi_y", "tmi_z"):
                    kernel_xx, kernel_yy, kernel_zz, kernel_xy, kernel_xz, kernel_yz = (
                        CHOCLO_KERNELS[component]
                    )
                    sensitivity_func = NUMBA_FUNCTIONS_2D["sensitivity"][
                        "tmi_derivative"
                    ][self.numba_parallel]
                    sensitivity_func(
                        receivers,
                        cells_bounds_active,
                        self.cell_z_top,
                        self.cell_z_bottom,
                        sensitivity_matrix[matrix_slice, :],
                        regional_field,
                        kernel_xx,
                        kernel_yy,
                        kernel_zz,
                        kernel_xy,
                        kernel_xz,
                        kernel_yz,
                        scalar_model,
                    )
                else:
                    kernel_x, kernel_y, kernel_z = CHOCLO_KERNELS[component]
                    sensitivity_func = NUMBA_FUNCTIONS_2D["sensitivity"][
                        "magnetic_component"
                    ][self.numba_parallel]
                    sensitivity_func(
                        receivers,
                        cells_bounds_active,
                        self.cell_z_top,
                        self.cell_z_bottom,
                        sensitivity_matrix[matrix_slice, :],
                        regional_field,
                        kernel_x,
                        kernel_y,
                        kernel_z,
                        scalar_model,
                    )
            index_offset += n_rows
        return sensitivity_matrix

    def _sensitivity_matrix_transpose_dot_vec(self, vector):
        """
        Compute ``G.T @ v`` without building ``G``.

        Parameters
        ----------
        vector : (nD) numpy.ndarray
            Vector used in the dot product.

        Returns
        -------
        (n_active_cells) or (3 * n_active_cells) numpy.ndarray
        """
        # Get regional field
        regional_field = self.survey.source_field.b0
        # Get cells in the 2D mesh and keep only active cells
        cells_bounds_active = self.mesh.cell_bounds[self.active_cells]
        # Allocate resulting array
        scalar_model = self.model_type == "scalar"
        result = np.zeros(self.nC if scalar_model else 3 * self.nC)
        # Start filling the result array
        index_offset = 0
        for components, receivers in self._get_components_and_receivers():
            if not CHOCLO_SUPPORTED_COMPONENTS.issuperset(components):
                raise NotImplementedError(
                    f"Other components besides {CHOCLO_SUPPORTED_COMPONENTS} "
                    "aren't implemented yet."
                )
            n_components = len(components)
            n_rows = n_components * receivers.shape[0]
            for i, component in enumerate(components):
                vector_slice = slice(
                    index_offset + i, index_offset + n_rows, n_components
                )
                if component == "tmi":
                    gt_dot_v_func = NUMBA_FUNCTIONS_2D["gt_dot_v"]["tmi"][
                        self.numba_parallel
                    ]
                    gt_dot_v_func(
                        receivers,
                        cells_bounds_active,
                        self.cell_z_top,
                        self.cell_z_bottom,
                        regional_field,
                        scalar_model,
                        vector[vector_slice],
                        result,
                    )
                elif component in ("tmi_x", "tmi_y", "tmi_z"):
                    kernel_xx, kernel_yy, kernel_zz, kernel_xy, kernel_xz, kernel_yz = (
                        CHOCLO_KERNELS[component]
                    )
                    gt_dot_v_func = NUMBA_FUNCTIONS_2D["gt_dot_v"]["tmi_derivative"][
                        self.numba_parallel
                    ]
                    gt_dot_v_func(
                        receivers,
                        cells_bounds_active,
                        self.cell_z_top,
                        self.cell_z_bottom,
                        regional_field,
                        kernel_xx,
                        kernel_yy,
                        kernel_zz,
                        kernel_xy,
                        kernel_xz,
                        kernel_yz,
                        scalar_model,
                        vector[vector_slice],
                        result,
                    )
                else:
                    kernel_x, kernel_y, kernel_z = CHOCLO_KERNELS[component]
                    gt_dot_v_func = NUMBA_FUNCTIONS_2D["gt_dot_v"][
                        "magnetic_component"
                    ][self.numba_parallel]
                    gt_dot_v_func(
                        receivers,
                        cells_bounds_active,
                        self.cell_z_top,
                        self.cell_z_bottom,
                        regional_field,
                        kernel_x,
                        kernel_y,
                        kernel_z,
                        scalar_model,
                        vector[vector_slice],
                        result,
                    )
            index_offset += n_rows
        return result

    def _gtg_diagonal_without_building_g(self, weights):
        """
        Compute the diagonal of ``G.T @ G`` without building the ``G`` matrix.

        Parameters
        -----------
        weights : (nD,) array
            Array with data weights. It should be the diagonal of the ``W``
            matrix, squared.

        Returns
        -------
        (n_active_cells) numpy.ndarray
        """
        # Get regional field
        regional_field = self.survey.source_field.b0
        # Get cells in the 2D mesh and keep only active cells
        cells_bounds_active = self.mesh.cell_bounds[self.active_cells]
        # Define the constant factor
        constant_factor = 1 / 4 / np.pi
        # Allocate array for the diagonal
        scalar_model = self.model_type == "scalar"
        n_columns = self.nC if scalar_model else 3 * self.nC
        diagonal = np.zeros(n_columns, dtype=np.float64)
        # Start filling the diagonal array
        for components, receivers in self._get_components_and_receivers():
            if not CHOCLO_SUPPORTED_COMPONENTS.issuperset(components):
                raise NotImplementedError(
                    f"Other components besides {CHOCLO_SUPPORTED_COMPONENTS} "
                    "aren't implemented yet."
                )
            for component in components:
                if component == "tmi":
                    diagonal_gtg_func = NUMBA_FUNCTIONS_2D["diagonal_gtg"]["tmi"][
                        self.numba_parallel
                    ]
                    diagonal_gtg_func(
                        receivers,
                        cells_bounds_active,
                        self.cell_z_top,
                        self.cell_z_bottom,
                        regional_field,
                        constant_factor,
                        scalar_model,
                        weights,
                        diagonal,
                    )
                elif component in ("tmi_x", "tmi_y", "tmi_z"):
                    kernel_xx, kernel_yy, kernel_zz, kernel_xy, kernel_xz, kernel_yz = (
                        CHOCLO_KERNELS[component]
                    )
                    diagonal_gtg_func = NUMBA_FUNCTIONS_2D["diagonal_gtg"][
                        "tmi_derivative"
                    ][self.numba_parallel]
                    diagonal_gtg_func(
                        receivers,
                        cells_bounds_active,
                        self.cell_z_top,
                        self.cell_z_bottom,
                        regional_field,
                        kernel_xx,
                        kernel_yy,
                        kernel_zz,
                        kernel_xy,
                        kernel_xz,
                        kernel_yz,
                        constant_factor,
                        scalar_model,
                        weights,
                        diagonal,
                    )
                else:
                    kernel_x, kernel_y, kernel_z = CHOCLO_KERNELS[component]
                    diagonal_gtg_func = NUMBA_FUNCTIONS_2D["diagonal_gtg"][
                        "magnetic_component"
                    ][self.numba_parallel]
                    diagonal_gtg_func(
                        receivers,
                        cells_bounds_active,
                        self.cell_z_top,
                        self.cell_z_bottom,
                        regional_field,
                        kernel_x,
                        kernel_y,
                        kernel_z,
                        constant_factor,
                        scalar_model,
                        weights,
                        diagonal,
                    )
        return diagonal


class Simulation3DDifferential(BaseMagneticPDESimulation):
    """
    Secondary field approach using differential equations!
    """

    def __init__(self, mesh, survey=None, **kwargs):
        super().__init__(mesh, survey=survey, **kwargs)

        Pbc, Pin, self._Pout = self.mesh.get_BC_projections(
            "neumann", discretization="CC"
        )

        Dface = self.mesh.face_divergence
        Mc = sdiag(self.mesh.cell_volumes)
        self._Div = Mc * Dface * Pin.T.tocsr() * Pin

    @property
    def survey(self):
        """The survey for this simulation.

        Returns
        -------
        simpeg.potential_fields.magnetics.survey.Survey
        """
        if self._survey is None:
            raise AttributeError("Simulation must have a survey")
        return self._survey

    @survey.setter
    def survey(self, obj):
        if obj is not None:
            obj = validate_type("survey", obj, Survey, cast=False)
            self._validate_survey(obj)
        self._survey = obj

    @property
    def MfMuI(self):
        return self._MfMuI

    @property
    def MfMui(self):
        return self._MfMui

    @property
    def MfMu0(self):
        return self._MfMu0

    def makeMassMatrices(self, m):
        mu = self.muMap * m
        self._MfMui = self.mesh.get_face_inner_product(1.0 / mu) / self.mesh.dim
        # self._MfMui = self.mesh.get_face_inner_product(1./mu)
        # TODO: this will break if tensor mu
        self._MfMuI = sdiag(1.0 / self._MfMui.diagonal())
        self._MfMu0 = self.mesh.get_face_inner_product(1.0 / mu_0) / self.mesh.dim
        # self._MfMu0 = self.mesh.get_face_inner_product(1/mu_0)

    @utils.requires("survey")
    def getB0(self):
        b0 = self.survey.source_field.b0
        B0 = np.r_[
            b0[0] * np.ones(self.mesh.nFx),
            b0[1] * np.ones(self.mesh.nFy),
            b0[2] * np.ones(self.mesh.nFz),
        ]
        return B0

    def getRHS(self, m):
        r"""

        .. math ::

            \mathbf{rhs} =
                \Div(\MfMui)^{-1}\mathbf{M}^f_{\mu_0^{-1}}\mathbf{B}_0
                - \Div\mathbf{B}_0
                +\diag(v)\mathbf{D} \mathbf{P}_{out}^T \mathbf{B}_{sBC}

        """
        B0 = self.getB0()

        mu = self.muMap * m
        chi = mu / mu_0 - 1

        # Temporary fix
        Bbc, Bbc_const = CongruousMagBC(self.mesh, self.survey.source_field.b0, chi)
        self.Bbc = Bbc
        self.Bbc_const = Bbc_const
        # return self._Div*self.MfMuI*self.MfMu0*B0 - self._Div*B0 +
        # Mc*Dface*self._Pout.T*Bbc
        return self._Div * self.MfMuI * self.MfMu0 * B0 - self._Div * B0

    def getA(self, m):
        r"""
        GetA creates and returns the A matrix for the Magnetics problem

        The A matrix has the form:

        .. math ::

            \mathbf{A} =  \Div(\MfMui)^{-1}\Div^{T}

        """
        return self._Div * self.MfMuI * self._Div.T.tocsr()

    def fields(self, m):
        r"""
        Return magnetic potential (u) and flux (B)

        u: defined on the cell center [nC x 1]
        B: defined on the cell center [nG x 1]

        After we compute u, then we update B.

        .. math ::

            \mathbf{B}_s =
                (\MfMui)^{-1}\mathbf{M}^f_{\mu_0^{-1}}\mathbf{B}_0
                - \mathbf{B}_0
                - (\MfMui)^{-1}\Div^T \mathbf{u}

        """
        self.makeMassMatrices(m)
        A = self.getA(m)
        rhs = self.getRHS(m)
        Ainv = self.solver(A, **self.solver_opts)
        u = Ainv * rhs
        B0 = self.getB0()
        B = self.MfMuI * self.MfMu0 * B0 - B0 - self.MfMuI * self._Div.T * u
        Ainv.clean()

        return {"B": B, "u": u}

    @utils.timeIt
    def Jvec(self, m, v, u=None):
        r"""
        Computing Jacobian multiplied by vector

        By setting our problem as

        .. math ::

            \mathbf{C}(\mathbf{m}, \mathbf{u}) = \mathbf{A}\mathbf{u} - \mathbf{rhs} = 0

        And taking derivative w.r.t m

        .. math ::

            \nabla \mathbf{C}(\mathbf{m}, \mathbf{u}) =
                \nabla_m \mathbf{C}(\mathbf{m}) \delta \mathbf{m} +
                \nabla_u \mathbf{C}(\mathbf{u}) \delta \mathbf{u} = 0

            \frac{\delta \mathbf{u}}{\delta \mathbf{m}} =
                - [\nabla_u \mathbf{C}(\mathbf{u})]^{-1}\nabla_m \mathbf{C}(\mathbf{m})

        With some linear algebra we can have

        .. math ::

            \nabla_u \mathbf{C}(\mathbf{u}) = \mathbf{A}

            \nabla_m \mathbf{C}(\mathbf{m}) =
                \frac{\partial \mathbf{A}} {\partial \mathbf{m}} (\mathbf{m}) \mathbf{u}
                - \frac{\partial \mathbf{rhs}(\mathbf{m})}{\partial \mathbf{m}}

        .. math ::

            \frac{\partial \mathbf{A}}{\partial \mathbf{m}}(\mathbf{m})\mathbf{u} =
                \frac{\partial \mathbf{\mu}}{\partial \mathbf{m}}
                \left[\Div \diag (\Div^T \mathbf{u}) \dMfMuI \right]

            \dMfMuI =
                \diag(\MfMui)^{-1}_{vec}
                \mathbf{Av}_{F2CC}^T\diag(\mathbf{v})\diag(\frac{1}{\mu^2})

            \frac{\partial \mathbf{rhs}(\mathbf{m})}{\partial \mathbf{m}} =
                \frac{\partial \mathbf{\mu}}{\partial \mathbf{m}}
                \left[
                    \Div \diag(\M^f_{\mu_{0}^{-1}}\mathbf{B}_0) \dMfMuI
                \right]
                - \diag(\mathbf{v}) \mathbf{D} \mathbf{P}_{out}^T
                    \frac{\partial B_{sBC}}{\partial \mathbf{m}}

        In the end,

        .. math ::

            \frac{\delta \mathbf{u}}{\delta \mathbf{m}} =
            - [ \mathbf{A} ]^{-1}
            \left[
                \frac{\partial \mathbf{A}}{\partial \mathbf{m}}(\mathbf{m})\mathbf{u}
                - \frac{\partial \mathbf{rhs}(\mathbf{m})}{\partial \mathbf{m}}
            \right]

        A little tricky point here is we are not interested in potential (u), but interested in magnetic flux (B).
        Thus, we need sensitivity for B. Now we take derivative of B w.r.t m and have

        .. math ::

            \frac{\delta \mathbf{B}} {\delta \mathbf{m}} =
            \frac{\partial \mathbf{\mu} } {\partial \mathbf{m} }
            \left[
                \diag(\M^f_{\mu_{0}^{-1} } \mathbf{B}_0) \dMfMuI  \
                 - \diag (\Div^T\mathbf{u})\dMfMuI
            \right ]

             -  (\MfMui)^{-1}\Div^T\frac{\delta\mathbf{u}}{\delta \mathbf{m}}

        Finally we evaluate the above, but we should remember that

        .. note ::

            We only want to evaluate

            .. math ::

                \mathbf{J}\mathbf{v} =
                    \frac{\delta \mathbf{P}\mathbf{B}} {\delta \mathbf{m}}\mathbf{v}

            Since forming sensitivity matrix is very expensive in that this
            monster is "big" and "dense" matrix!!

        """
        if u is None:
            u = self.fields(m)

        B, u = u["B"], u["u"]
        mu = self.muMap * (m)
        dmu_dm = self.muDeriv
        # dchidmu = sdiag(1 / mu_0 * np.ones(self.mesh.nC))

        vol = self.mesh.cell_volumes
        Div = self._Div
        P = self.survey.projectFieldsDeriv(B)  # Projection matrix
        B0 = self.getB0()

        MfMuIvec = 1 / self.MfMui.diagonal()
        dMfMuI = sdiag(MfMuIvec**2) * self.mesh.aveF2CC.T * sdiag(vol * 1.0 / mu**2)

        # A = self._Div*self.MfMuI*self._Div.T
        # RHS = Div*MfMuI*MfMu0*B0 - Div*B0 + Mc*Dface*Pout.T*Bbc
        # C(m,u) = A*m-rhs
        # dudm = -(dCdu)^(-1)dCdm

        dCdu = self.getA(m)  # = A
        dCdm_A = Div * (sdiag(Div.T * u) * dMfMuI * dmu_dm)
        dCdm_RHS1 = Div * (sdiag(self.MfMu0 * B0) * dMfMuI)
        # temp1 = (Dface * (self._Pout.T * self.Bbc_const * self.Bbc))
        # dCdm_RHS2v = (sdiag(vol) * temp1) * \
        #    np.inner(vol, dchidmu * dmu_dm * v)

        # dCdm_RHSv =  dCdm_RHS1*(dmu_dm*v) +  dCdm_RHS2v
        dCdm_RHSv = dCdm_RHS1 * (dmu_dm * v)
        dCdm_v = dCdm_A * v - dCdm_RHSv

        Ainv = self.solver(dCdu, **self.solver_opts)
        sol = Ainv * dCdm_v

        dudm = -sol
        dBdmv = (
            sdiag(self.MfMu0 * B0) * (dMfMuI * (dmu_dm * v))
            - sdiag(Div.T * u) * (dMfMuI * (dmu_dm * v))
            - self.MfMuI * (Div.T * (dudm))
        )

        Ainv.clean()

        return mkvc(P * dBdmv)

    @utils.timeIt
    def Jtvec(self, m, v, u=None):
        r"""
        Computing Jacobian^T multiplied by vector.

        .. math ::

            (\frac{\delta \mathbf{P}\mathbf{B}} {\delta \mathbf{m}})^{T} =
                \left[
                    \mathbf{P}_{deriv}\frac{\partial \mathbf{\mu} } {\partial \mathbf{m} }
                    \left[
                        \diag(\M^f_{\mu_{0}^{-1} } \mathbf{B}_0) \dMfMuI
                         - \diag (\Div^T\mathbf{u})\dMfMuI
                    \right ]
                \right]^{T}
                 -
                 \left[
                     \mathbf{P}_{deriv}(\MfMui)^{-1} \Div^T
                     \frac{\delta\mathbf{u}}{\delta \mathbf{m}}
                 \right]^{T}

        where

        .. math ::

            \mathbf{P}_{derv} = \frac{\partial \mathbf{P}}{\partial\mathbf{B}}

        .. note ::

            Here we only want to compute

            .. math ::

                \mathbf{J}^{T}\mathbf{v} =
                (\frac{\delta \mathbf{P}\mathbf{B}} {\delta \mathbf{m}})^{T} \mathbf{v}

        """
        if u is None:
            u = self.fields(m)

        B, u = u["B"], u["u"]
        mu = self.mapping * (m)
        dmu_dm = self.mapping.deriv(m)
        # dchidmu = sdiag(1 / mu_0 * np.ones(self.mesh.nC))

        vol = self.mesh.cell_volumes
        Div = self._Div
        P = self.survey.projectFieldsDeriv(B)  # Projection matrix
        B0 = self.getB0()

        MfMuIvec = 1 / self.MfMui.diagonal()
        dMfMuI = sdiag(MfMuIvec**2) * self.mesh.aveF2CC.T * sdiag(vol * 1.0 / mu**2)

        # A = self._Div*self.MfMuI*self._Div.T
        # RHS = Div*MfMuI*MfMu0*B0 - Div*B0 + Mc*Dface*Pout.T*Bbc
        # C(m,u) = A*m-rhs
        # dudm = -(dCdu)^(-1)dCdm

        dCdu = self.getA(m)
        s = Div * (self.MfMuI.T * (P.T * v))

        Ainv = self.solver(dCdu.T, **self.solver_opts)
        sol = Ainv * s

        Ainv.clean()

        # dCdm_A = Div * ( sdiag( Div.T * u )* dMfMuI *dmu_dm  )
        # dCdm_Atsol = ( dMfMuI.T*( sdiag( Div.T * u ) * (Div.T * dmu_dm)) ) * sol
        dCdm_Atsol = (dmu_dm.T * dMfMuI.T * (sdiag(Div.T * u) * Div.T)) * sol

        # dCdm_RHS1 = Div * (sdiag( self.MfMu0*B0  ) * dMfMuI)
        # dCdm_RHS1tsol = (dMfMuI.T*( sdiag( self.MfMu0*B0  ) ) * Div.T * dmu_dm) * sol
        dCdm_RHS1tsol = (dmu_dm.T * dMfMuI.T * (sdiag(self.MfMu0 * B0)) * Div.T) * sol

        # temp1 = (Dface*(self._Pout.T*self.Bbc_const*self.Bbc))
        # temp1sol = (Dface.T * (sdiag(vol) * sol))
        # temp2 = self.Bbc_const * (self._Pout.T * self.Bbc).T
        # dCdm_RHS2v  = (sdiag(vol)*temp1)*np.inner(vol, dchidmu*dmu_dm*v)
        # dCdm_RHS2tsol = (dmu_dm.T * dchidmu.T * vol) * np.inner(temp2, temp1sol)

        # dCdm_RHSv =  dCdm_RHS1*(dmu_dm*v) +  dCdm_RHS2v

        # temporary fix
        # dCdm_RHStsol = dCdm_RHS1tsol - dCdm_RHS2tsol
        dCdm_RHStsol = dCdm_RHS1tsol

        # dCdm_RHSv =  dCdm_RHS1*(dmu_dm*v) +  dCdm_RHS2v
        # dCdm_v = dCdm_A*v - dCdm_RHSv

        Ctv = dCdm_Atsol - dCdm_RHStsol

        # B = self.MfMuI*self.MfMu0*B0-B0-self.MfMuI*self._Div.T*u
        # dBdm = d\mudm*dBd\mu
        # dPBdm^T*v = Atemp^T*P^T*v - Btemp^T*P^T*v - Ctv

        Atemp = sdiag(self.MfMu0 * B0) * (dMfMuI * (dmu_dm))
        Btemp = sdiag(Div.T * u) * (dMfMuI * (dmu_dm))
        Jtv = Atemp.T * (P.T * v) - Btemp.T * (P.T * v) - Ctv

        return mkvc(Jtv)

    @property
    def Qfx(self):
        if getattr(self, "_Qfx", None) is None:
            self._Qfx = self.mesh.get_interpolation_matrix(
                self.survey.receiver_locations, "Fx"
            )
        return self._Qfx

    @property
    def Qfy(self):
        if getattr(self, "_Qfy", None) is None:
            self._Qfy = self.mesh.get_interpolation_matrix(
                self.survey.receiver_locations, "Fy"
            )
        return self._Qfy

    @property
    def Qfz(self):
        if getattr(self, "_Qfz", None) is None:
            self._Qfz = self.mesh.get_interpolation_matrix(
                self.survey.receiver_locations, "Fz"
            )
        return self._Qfz

    def projectFields(self, u):
        r"""
        This function projects the fields onto the data space.
        Especially, here for we use total magnetic intensity (TMI) data,
        which is common in practice.
        First we project our B on to data location

        .. math::

            \mathbf{B}_{rec} = \mathbf{P} \mathbf{B}

        then we take the dot product between B and b_0

        .. math ::

            \text{TMI} = \vec{B}_s \cdot \hat{B}_0

        """
        # Get components for all receivers, assuming they all have the same components
        components = self._get_components()

        fields = {}
        if "bx" in components or "tmi" in components:
            fields["bx"] = self.Qfx * u["B"]
        if "by" in components or "tmi" in components:
            fields["by"] = self.Qfy * u["B"]
        if "bz" in components or "tmi" in components:
            fields["bz"] = self.Qfz * u["B"]

        if "tmi" in components:
            bx = fields["bx"]
            by = fields["by"]
            bz = fields["bz"]
            # Generate unit vector
            B0 = self.survey.source_field.b0
            Bot = np.sqrt(B0[0] ** 2 + B0[1] ** 2 + B0[2] ** 2)
            box = B0[0] / Bot
            boy = B0[1] / Bot
            boz = B0[2] / Bot
            fields["tmi"] = bx * box + by * boy + bz * boz

        return np.concatenate([fields[comp] for comp in components])

    @utils.count
    def projectFieldsDeriv(self, B):
        r"""
        This function projects the fields onto the data space.

        .. math::

            \frac{\partial d_\text{pred}}{\partial \mathbf{B}} = \mathbf{P}

        Especially, this function is for TMI data type
        """
        # Get components for all receivers, assuming they all have the same components
        components = self._get_components()

        fields = {}
        if "bx" in components or "tmi" in components:
            fields["bx"] = self.Qfx
        if "by" in components or "tmi" in components:
            fields["by"] = self.Qfy
        if "bz" in components or "tmi" in components:
            fields["bz"] = self.Qfz

        if "tmi" in components:
            bx = fields["bx"]
            by = fields["by"]
            bz = fields["bz"]
            # Generate unit vector
            B0 = self.survey.source_field.b0
            Bot = np.sqrt(B0[0] ** 2 + B0[1] ** 2 + B0[2] ** 2)
            box = B0[0] / Bot
            boy = B0[1] / Bot
            boz = B0[2] / Bot
            fields["tmi"] = bx * box + by * boy + bz * boz

        return sp.vstack([fields[comp] for comp in components])

    def _get_components(self):
        """
        Get components of all receivers in the survey.

        This function assumes that all receivers in the survey have the same
        components in the same order.

        Returns
        -------
        components : list of str
            List of components shared by all receivers in the survey.

        Raises
        ------
        ValueError
            If the survey doesn't have any receiver, or if any receiver has
            a different set of components than the rest.
        """
        # Validate survey first to ensure that the receivers have all the same
        # components.
        self._validate_survey(self.survey)
        components = self.survey.source_field.receiver_list[0].components
        return components

    def _validate_survey(self, survey):
        """
        Validate a survey for the magnetic differential 3D simulation.

        Parameters
        ----------
        survey : Survey
            Survey object that will get validated.

        Raises
        ------
        ValueError
            If the survey doesn't have any receiver, or if any receiver has
            a different set of components than the rest.
        """
        receivers = survey.source_field.receiver_list
        if not receivers:
            msg = "Found invalid survey without receivers."
            raise ValueError(msg)
        components = receivers[0].components
        if not all(components == rx.components for rx in receivers):
            msg = (
                "Found invalid survey with receivers that have mixed components. "
                f"Surveys for the {type(self).__name__} class must contain receivers "
                "with the same components."
            )
            raise ValueError(msg)

    def projectFieldsAsVector(self, B):
        bfx = self.Qfx * B
        bfy = self.Qfy * B
        bfz = self.Qfz * B

        return np.r_[bfx, bfy, bfz]


def MagneticsDiffSecondaryInv(mesh, model, data, **kwargs):
    """
    Inversion module for MagneticsDiffSecondary

    """
    from simpeg import (
        directives,
        inversion,
        objective_function,
        optimization,
        regularization,
    )

    prob = Simulation3DDifferential(mesh, survey=data, mu=model)

    miter = kwargs.get("maxIter", 10)

    # Create an optimization program
    opt = optimization.InexactGaussNewton(maxIter=miter)
    opt.bfgsH0 = get_default_solver(warn=True)(sp.identity(model.nP), flag="D")
    # Create a regularization program
    reg = regularization.WeightedLeastSquares(model)
    # Create an objective function
    beta = directives.BetaSchedule(beta0=1e0)
    obj = objective_function.BaseObjFunction(prob, reg, beta=beta)
    # Create an inversion object
    inv = inversion.BaseInversion(obj, opt)

    return inv, reg

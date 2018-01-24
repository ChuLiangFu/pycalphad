"""
The utils module handles helper routines for equilibrium calculation.
"""
from __future__ import division
import pycalphad.variables as v
from pycalphad.core.halton import halton
from pycalphad.core.constants import MIN_SITE_FRACTION
from sympy.utilities.lambdify import lambdify
from sympy.printing.lambdarepr import LambdaPrinter
import numpy as np
import operator
import functools
import itertools
import collections

try:
    # Only available in numpy 1.10 and newer
    from numpy import broadcast_to
except ImportError:
    def broadcast_to(arr, shape):
        "Broadcast an array to a desired shape. Returns a view."
        return np.broadcast_arrays(arr, np.empty(shape, dtype=np.bool))[0]


def point_sample(comp_count, pdof=10):
    """
    Sample 'pdof * (sum(comp_count) - len(comp_count))' points in
    composition space for the sublattice configuration specified
    by 'comp_count'. Points are sampled quasi-randomly from a Halton sequence.
    A Halton sequence is like a uniform random distribution, but the
    result will always be the same for a given 'comp_count' and 'pdof'.
    Note: For systems with only one component, only one point will be
    returned, regardless of 'pdof'. This is because the degrees of freedom
    are zero for that case.

    Parameters
    ----------
    comp_count : list
        Number of components in each sublattice.
    pdof : int
        Number of points to sample per degree of freedom.

    Returns
    -------
    ndarray of generated points satisfying the mass balance.

    Examples
    --------
    >>> comps = [8,1] # 8 components in sublattice 1; only 1 in sublattice 2
    >>> pts = point_sample(comps, pdof=20) # 7 d.o.f, returns a 140x7 ndarray
    """
    # Generate Halton sequence with appropriate dimensions and size
    pts = halton(sum(comp_count),
                 pdof * (sum(comp_count) - len(comp_count)), scramble=True)
    # Convert low-discrepancy sequence to normalized exponential
    # This will be uniformly distributed over the simplices
    pts = -np.log(pts)
    cur_idx = 0
    for ctx in comp_count:
        end_idx = cur_idx + ctx
        pts[:, cur_idx:end_idx] /= pts[:, cur_idx:end_idx].sum(axis=1)[:, None]
        cur_idx = end_idx

    if len(pts) == 0:
        pts = np.atleast_2d([1] * len(comp_count))
    return pts

def make_callable(model, variables, mode=None):
    """
    Take a SymPy object and create a callable function.

    Parameters
    ----------
    model, SymPy object
        Abstract representation of function
    variables, list
        Input variables, ordered in the way the return function will expect
    mode, ['numpy', 'numba', 'sympy'], optional
        Method to use when 'compiling' the function. SymPy mode is
        slow and should only be used for debugging. If Numba is installed,
        it can offer speed-ups when calling the energy function many
        times on multi-core CPUs.

    Returns
    -------
    Function that takes arguments in the same order as 'variables'
    and returns the energy according to 'model'.

    Examples
    --------
    None yet.
    """
    energy = None
    if mode is None:
        mode = 'numpy'

    if mode == 'sympy':
        energy = lambda *vs: model.subs(zip(variables, vs)).evalf()
    else:
        energy = lambdify(tuple(variables), model, dummify=True,
                          modules=mode)

    return energy

def sizeof_fmt(num, suffix='B'):
    """
    Human-readable string for a number of bytes.
    http://stackoverflow.com/questions/1094841/reusable-library-to-get-human-readable-version-of-file-size
    """
    for unit in ['', 'K', 'M', 'G', 'T', 'P', 'E', 'Z']:
        if abs(num) < 1000.0:
            return "%3.1f%s%s" % (num, unit, suffix)
        num /= 1000.0
    return "%.1f%s%s" % (num, 'Y', suffix)

def unpack_condition(tup):
    """
    Convert a condition to a list of values.

    Notes
    -----
    Rules for keys of conditions dicts:
    (1) If it's numeric, treat as a point value
    (2) If it's a tuple with one element, treat as a point value
    (3) If it's a tuple with two elements, treat as lower/upper limits and guess a step size.
    (4) If it's a tuple with three elements, treat as lower/upper/step
    (5) If it's a list, ndarray or other non-tuple ordered iterable, use those values directly.

    """
    if isinstance(tup, tuple):
        if len(tup) == 1:
            return [float(tup[0])]
        elif len(tup) == 2:
            return np.arange(tup[0], tup[1], dtype=np.float)
        elif len(tup) == 3:
            return np.arange(tup[0], tup[1], tup[2], dtype=np.float)
        else:
            raise ValueError('Condition tuple is length {}'.format(len(tup)))
    elif isinstance(tup, collections.Iterable):
        return [float(x) for x in tup]
    else:
        return [float(tup)]

def unpack_phases(phases):
    "Convert a phases list/dict into a sorted list."
    active_phases = None
    if isinstance(phases, (list, tuple, set)):
        active_phases = sorted(phases)
    elif isinstance(phases, dict):
        active_phases = sorted([phn for phn, status in phases.items() \
            if status == 'entered'])
    elif type(phases) is str:
        active_phases = [phases]
    return active_phases

def generate_dof(phase, active_comps):
    """
    Accept a Phase object and a set() of the active components.
    Return a tuple of variable names and the sublattice degrees of freedom.
    """
    variables = []
    sublattice_dof = []
    for idx, sublattice in enumerate(phase.constituents):
        dof = 0
        for component in sorted(set(sublattice).intersection(active_comps)):
            variables.append(v.SiteFraction(phase.name.upper(), idx, component))
            dof += 1
        sublattice_dof.append(dof)
    return variables, sublattice_dof

def endmember_matrix(dof, vacancy_indices=None):
    """
    Accept the number of components in each sublattice.
    Return a matrix corresponding to the compositions of all endmembers.

    Parameters
    ----------
    dof : list of int
        Number of components in each sublattice.
    vacancy_indices, list of int, optional
        If vacancies are present in every sublattice, specify their indices
        in each sublattice to ensure the "pure vacancy" endmember is excluded.

    Examples
    --------
    Sublattice configuration like: `(AL, NI, VA):(AL, NI, VA):(VA)`
    >>> endmember_matrix([3,3,1], vacancy_indices=[2, 2, 0])
    """
    total_endmembers = functools.reduce(operator.mul, dof, 1)
    res_matrix = np.empty((total_endmembers, sum(dof)), dtype=np.float)
    dof_arrays = [np.eye(d).tolist() for d in dof]
    row_idx = 0
    for row in itertools.product(*dof_arrays):
        res_matrix[row_idx, :] = np.concatenate(row, axis=0)
        row_idx += 1
    if vacancy_indices is not None and len(vacancy_indices) == len(dof):
        dof_adj = np.array([sum(dof[0:i]) for i in range(len(dof))])
        indices = np.array(vacancy_indices) + dof_adj
        row_idx_to_delete = np.where(np.all(res_matrix[:, indices] == 1,
                                            axis=1))
        res_matrix = np.delete(res_matrix, (row_idx_to_delete), axis=0)
    # Adjust site fractions to the numerical limit
    cur_idx = 0
    res_matrix[res_matrix == 0] = MIN_SITE_FRACTION
    for ctx in dof:
        end_idx = cur_idx + ctx
        res_matrix[:, cur_idx:end_idx] /= res_matrix[:, cur_idx:end_idx].sum(axis=1)[:, None]
        cur_idx = end_idx
    return res_matrix

def unpack_kwarg(kwarg_obj, default_arg=None):
    """
    Keyword arguments in pycalphad can be passed as a constant value, a
    dict of phase names and values, or a list containing both of these. If
    the latter, then the dict is checked first; if the phase of interest is not
    there, then the constant value is used.

    This function is a way to construct defaultdicts out of keyword arguments.

    Parameters
    ----------
    kwarg_obj : dict, iterable, or None
        Argument to unpack into a defaultdict
    default_arg : object
        Default value to use if iterable isn't specified

    Returns
    -------
    defaultdict for the keyword argument of interest

    Examples
    --------
    >>> test_func = lambda **kwargs: print(unpack_kwarg('opt'))
    >>> test_func(opt=100)
    >>> test_func(opt={'FCC_A1': 50, 'BCC_B2': 10})
    >>> test_func(opt=[{'FCC_A1': 30}, 200])
    >>> test_func()
    >>> test_func2 = lambda **kwargs: print(unpack_kwarg('opt', default_arg=1))
    >>> test_func2()
    """
    new_dict = collections.defaultdict(lambda: default_arg)

    if isinstance(kwarg_obj, collections.Mapping):
        new_dict.update(kwarg_obj)
    # kwarg_obj is a list containing a dict and a default
    # For now at least, we don't treat ndarrays the same as other iterables
    # ndarrays are assumed to be numeric arrays containing "default values", so don't match here
    elif isinstance(kwarg_obj, collections.Iterable) and not isinstance(kwarg_obj, np.ndarray):
        for element in kwarg_obj:
            if isinstance(element, collections.Mapping):
                new_dict.update(element)
            else:
                # element=element syntax to silence var-from-loop warning
                new_dict = collections.defaultdict(
                    lambda element=element: element, new_dict)
    elif kwarg_obj is None:
        pass
    else:
        new_dict = collections.defaultdict(lambda: kwarg_obj)

    return new_dict


def unpack_components(dbf, comps):
    """

    Parameters
    ----------
    dbf : Database
        Thermodynamic database containing elements and species.
    comps : list
        Names of components to consider in the calculation.

    Returns
    -------
    set
        Set of Species objects
    """
    # Constrain possible components to those within phase's d.o.f
    # Assume for the moment that comps contains a list of pure element strings
    # We want to add all the species which can be created by a combination of
    # the user-specified pure elements
    possible_comps = {v.Species(x) for x in comps}
    desired_active_pure_elements = [list(x.constituents.keys()) for x in possible_comps]
    # Flatten nested list
    desired_active_pure_elements = [el.upper() for constituents in desired_active_pure_elements for el in constituents]
    eligible_species_from_database = {x for x in dbf.species if
                                      set(x.constituents.keys()).issubset(desired_active_pure_elements)}
    return eligible_species_from_database


def filter_phases(dbf, comps):
    """Return phases that are valid for equilibrium calculations for the given database and components

    Filters out phases that
    * Have no active components in any sublattice of a phase
    * Are disordered phases in an order-disorder model

    Parameters
    ----------
    dbf : Database
        Thermodynamic database containing the relevant parameters.
    comps : list
        Names of components to consider in the calculation.

    Returns
    -------
    list
        Sorted list of phases that are valid for the Database and components
    """
    # TODO: filter phases that can not charge balance

    def all_sublattices_active(comps, phase):
        active_sublattices = [len(set(comps).intersection(subl)) > 0 for
                              subl in phase.constituents]
        return all(active_sublattices)

    candidate_phases = dbf.phases.keys()
    disordered_phases = [dbf.phases[phase].model_hints.get('disordered_phase') for phase in candidate_phases]
    phases = [phase for phase in candidate_phases if
                all_sublattices_active(comps, dbf.phases[phase]) and
                phase not in disordered_phases]
    return sorted(phases)

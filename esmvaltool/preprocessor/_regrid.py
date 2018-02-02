"""
A package for performing horizontal regridding, and vertical level extraction
or vertical level interpolation.

"""

from __future__ import absolute_import, division, print_function

import re
from copy import deepcopy
from ..preprocessor._reformat import CMOR_TABLES

import iris
import numpy as np
import six
import stratify
from iris.analysis import AreaWeighted, Linear, Nearest, UnstructuredNearest
from numpy import ma
import os

# Regular expression to parse a "MxN" cell-specification.
_CELL_SPEC = re.compile(r'''\A
                            \s*(?P<dx>\d+(\.\d+)?)\s*
                            x
                            \s*(?P<dy>\d+(\.\d+)?)\s*
                            \Z
                         ''', re.IGNORECASE | re.VERBOSE)

# Default fill-value.
_MDI = 1e+20

# Stock cube - global grid extents (degrees).
_LAT_MIN = -90.0
_LAT_MAX = 90.0
_LAT_RANGE = _LAT_MAX - _LAT_MIN
_LON_MIN = 0.0
_LON_MAX = 360.0
_LON_RANGE = _LON_MAX - _LON_MIN

# A cached stock of standard horizontal target grids.
_cache = dict()

# Supported horizontal regridding schemes.
horizontal_schemes = dict(
    linear=Linear(extrapolation_mode='mask'),
    nearest=Nearest(extrapolation_mode='mask'),
    area_weighted=AreaWeighted(),
    unstructured_nearest=UnstructuredNearest())

# Supported vertical interpolation schemes.
vertical_schemes = ['linear', 'nearest']


def _stock_cube(spec):
    """
    Create a global cube with M degree-east by N degree-north regular grid
    cells.

    The longitude range is from 0 to 360 degrees. The latitude range is from
    -90 to 90 degrees. Each cell grid point is calculated as the mid-point of
    the associated MxN cell.

    Paramaters
    ----------
    spec : str
        Specifies the 'MxN' degree cell-specification for the global grid.

    Returns
    -------
        A :class:`~iris.cube.Cube`.

    """
    # Parse the MxN cell specification string.
    cell_match = _CELL_SPEC.match(spec)
    if cell_match is None:
        emsg = 'Invalid MxN cell specification for stock cube, got {!r}.'
        raise ValueError(emsg.format(spec))

    cell_group = cell_match.groupdict()
    dx = float(cell_group['dx'])
    dy = float(cell_group['dy'])

    if (np.trunc(_LON_RANGE / dx) * dx) != _LON_RANGE:
        emsg = ('Invalid longitude delta in MxN cell specification '
                'for stock cube, got {!r}.')
        raise ValueError(emsg.format(dx))

    if (np.trunc(_LAT_RANGE / dy) * dy) != _LAT_RANGE:
        emsg = ('Invalid latitude delta in MxN cell specification '
                'for stock cube, got {!r}.')
        raise ValueError(emsg.format(dy))

    mid_dx, mid_dy = dx / 2, dy / 2

    # Construct the latitude coordinate, with bounds.
    ydata = np.linspace(_LAT_MIN + mid_dy, _LAT_MAX - mid_dy, _LAT_RANGE / dy)
    lats = iris.coords.DimCoord(
        ydata, standard_name='latitude', units='degrees_north')
    lats.guess_bounds()

    # Construct the longitude coordinate, with bounds.
    xdata = np.linspace(_LON_MIN + mid_dx, _LON_MAX - mid_dx, _LON_RANGE / dx)
    lons = iris.coords.DimCoord(
        xdata, standard_name='longitude', units='degrees_east')
    lons.guess_bounds()

    # Construct the resultant stock cube, with dummy data.
    shape = (ydata.size, xdata.size)
    dummy = np.empty(shape, dtype=np.dtype('int8'))
    coords_spec = [(lats, 0), (lons, 1)]
    cube = iris.cube.Cube(dummy, dim_coords_and_dims=coords_spec)

    return cube


def regrid(src_cube, target_grid, scheme):
    """
    Perform horizontal regridding.

    Parameters
    ----------
    src_cube : cube
        The source cube to be regridded.
    tgt_cube : cube or str
        The cube that specifies the target or reference grid for the regridding
        operation. Alternatively, a string cell specification may be provided,
        of the form 'MxN', which specifies the extent of the cell, longitude by
        latitude (degrees) for a global, regular target grid.
    scheme : str
        The regridding scheme to perform, see `regrid.horizontal_schemes`.

    Returns
    -------
    cube

    See Also
    --------
    vinterp : Perform vertical regridding.

    """
    if target_grid is None and scheme is None:
        # nop
        return src_cube

    if target_grid is None:
        emsg = 'A target grid must be specified for horizontal regridding.'
        raise ValueError(emsg)

    if scheme is None:
        emsg = 'A scheme must be specified for horizontal regridding.'
        raise ValueError(emsg)

    if horizontal_schemes.get(scheme.lower()) is None:
        emsg = 'Unknown regridding scheme, got {!r}.'
        raise ValueError(emsg.format(scheme))

    if isinstance(target_grid, six.string_types):
        if os.path.isfile(target_grid):
            target_grid = iris.load_cube(target_grid)
        else:
            # Generate a target grid from the provided cell-specification,
            # and cache the resulting stock cube for later use.
            target_grid = _cache.setdefault(target_grid,
                                            _stock_cube(target_grid))
            # Align the target grid coordinate system to the source
            # coordinate system.
            src_cs = src_cube.coord_system()
            xcoord = target_grid.coord(axis='x', dim_coords=True)
            ycoord = target_grid.coord(axis='y', dim_coords=True)
            xcoord.coord_system = src_cs
            ycoord.coord_system = src_cs

    if not isinstance(target_grid, iris.cube.Cube):
        emsg = 'Expecting a cube or cell-specification, got {}.'
        raise ValueError(emsg.format(type(target_grid)))

    # Unstructured regridding requires x2 2d spatial coordinates,
    # so ensure to purge any 1d native spatial dimension coordinates
    # for the regridder.
    if scheme == 'unstructured_nearest':
        for axis in ['x', 'y']:
            coords = src_cube.coords(axis=axis, dim_coords=True)
            if coords:
                [coord] = coords
                src_cube.remove_coord(coord)

    # Perform the horizontal regridding.
    result = src_cube.regrid(target_grid, horizontal_schemes[scheme])

    return result


def _create_cube(src_cube, data, levels):
    """
    Generate a new cube with the interpolated data.

    The resultant cube is seeded with `src_cube` metadata and coordinates,
    excluding any source coordinates that span the associated vertical
    dimension. The `levels` of interpolation are used along with the
    associated source cube vertical coordinate metadata to add a new
    vertical coordinate to the resultant cube.

    Parameters
    ----------
    src_cube : cube
        The source cube that was vertically interpolated.
    data : array
        The payload resulting from interpolating the source cube
        over the specified levels.
    levels : array
        The vertical levels of interpolation.

    Returns
    -------
    cube

    .. note::

        If there is only one level of interpolation, the resultant cube
        will be collapsed over the associated vertical dimension, and a
        scalar vertical coordinate will be added.

    """
    # Get the source cube vertical coordinate and associated dimension.
    src_levels = src_cube.coord(axis='z', dim_coords=True)
    z_dim, = src_cube.coord_dims(src_levels)

    if data.shape[z_dim] != levels.size:
        emsg = ('Mismatch between data and levels for data dimension {!r}, '
                'got data shape {!r} with levels shape {!r}.')
        raise ValueError(emsg.format(z_dim, data.shape, levels.shape))

    # Construct the resultant cube with the interpolated data
    # and the source cube metadata.
    kwargs = deepcopy(src_cube.metadata)._asdict()
    result = iris.cube.Cube(data, **kwargs)

    # Add the appropriate coordinates to the cube, excluding
    # any coordinates that span the z-dimension of interpolation.
    for coord in src_cube.dim_coords:
        [dim] = src_cube.coord_dims(coord)
        if dim != z_dim:
            result.add_dim_coord(coord.copy(), dim)

    for coord in src_cube.aux_coords:
        dims = src_cube.coord_dims(coord)
        if z_dim not in dims:
            result.add_aux_coord(coord.copy(), dims)

    for coord in src_cube.derived_coords:
        dims = src_cube.coord_dims(coord)
        if z_dim not in dims:
            result.add_aux_coord(coord.copy(), dims)

    # Construct the new vertical coordinate for the interpolated
    # z-dimension, using the associated source coordinate metadata.
    kwargs = deepcopy(src_levels._as_defn())._asdict()

    try:
        coord = iris.coords.DimCoord(levels, **kwargs)
        result.add_dim_coord(coord, z_dim)
    except ValueError:
        coord = iris.coords.AuxCoord(levels, **kwargs)
        result.add_aux_coord(coord, z_dim)

    # Collapse the z-dimension for the scalar case.
    if levels.size == 1:
        slicer = [slice(None)] * result.ndim
        slicer[z_dim] = 0
        result = result[tuple(slicer)]

    return result


def vinterp(src_cube, levels, scheme):
    """
    Perform vertical interpolation.

    Paramaters
    ----------
    src_cube : cube
        The source cube to be vertically interpolated.
    levels : array
        One or more target levels for the vertical interpolation. Assumed
        to be in the same S.I. units of the source cube vertical dimension
        coordinate.
    scheme : str
        The vertical interpolation scheme to perform. Currently supported
        schemes are 'linear' or 'nearest'.

    Returns
    -------
    cube

    See Also
    --------
    regrid : Perform horizontal regridding.

    """
    # Default to passing thru the original source cube.
    result = src_cube

    if levels is None and scheme is None:
        # nop
        return src_cube

    if levels is None:
        emsg = 'Target levels must be specified for vertical interpolation.'
        raise ValueError(emsg)

    if scheme is None:
        emsg = 'A scheme must be specified for vertical interpolation.'
        raise ValueError(emsg)

    if scheme not in vertical_schemes:
        emsg = 'Unknown vertical interpolation scheme, got {!r}.'
        raise ValueError(emsg.format(scheme))

    if isinstance(levels, six.string_types):
        levels = get_cmor_levels(levels)

    # Ensure we have a non-scalar array of levels.
    levels = np.array(levels, ndmin=1)

    # Get the source cube vertical coordinate, if available.
    src_levels = src_cube.coord(axis='z', dim_coords=True)

    # Only perform vertical extraction/interploation if the source
    # and target levels are not "similar" enough.
    if src_levels.shape != levels.shape or \
       not np.allclose(src_levels.points, levels):

        # Determine whether we can simply extract the target levels,
        # if they *all* exist in the source cube, otherwise
        # perform vertical interpolation.
        if set(levels).issubset(set(src_levels.points)):
            name = src_levels.name()
            coord_values = {name: lambda cell: cell.point in set(levels)}
            constraint = iris.Constraint(coord_values=coord_values)
            result = src_cube.extract(constraint)

            # Ensure the constraint did not fail.
            if not isinstance(result, iris.cube.Cube):
                emsg = 'Failed to extract levels {!r} from cube {!r}.'
                raise ValueError(emsg.format(list(levels), name))
        else:
            # Determine the source axis for vertical interpolation.
            z_axis, = src_cube.coord_dims(src_levels)

            # Broadcast the 1d source cube vertical coordinate to fully
            # describe the spatial extent that will be interpolated.
            broadcast_shape = src_cube.shape[z_axis:]
            reshape = [1] * len(broadcast_shape)
            reshape[0] = src_cube.shape[z_axis]
            src_levels_reshaped = src_levels.points.reshape(reshape)
            src_levels_broadcast = np.broadcast_to(src_levels_reshaped,
                                                   broadcast_shape)

            # Now perform the actual vertical interpolation.
            new_data = stratify.interpolate(
                levels,
                src_levels_broadcast,
                src_cube.data,
                axis=z_axis,
                interpolation=scheme,
                extrapolation='nan')

            # Determine if we need to fill any extrapolated NaN values.
            mask = np.isnan(new_data)

            if np.any(mask):
                # Replace the NaN values with the fill-value.
                new_data[mask] = _MDI

            # Ensure that any spatial mask is re-applied.
            if ma.isMaskedArray(src_cube.data):
                slicer = tuple([0] * (z_axis + 1))
                # Assume that the spatial mask is invariant.
                mask = src_cube.data.mask[slicer]
                mask = np.broadcast_to(mask, new_data.shape)
                new_data = ma.array(new_data, mask=mask)

            # Construct the resulting cube with the interpolated data.
            result = _create_cube(src_cube, new_data, levels)

    return result


def get_cmor_levels(levels):
    """
    Get level definition from a CMOR coordinate

    Parameters
    ----------
    levels: str
        String in the format {CMOR_TABLE}_{COORDINATE_NAME}

    Returns
    -------
    list[int]

    Raises
    ------
    ValueError:
        If the CMOR table is not defined, the coordinata does not specify any
        levels or the string is badly formatted
    """
    level_definition = levels.split('_')
    cmor_type = level_definition[0]

    if cmor_type not in CMOR_TABLES:
        raise ValueError('Level definition {} not available'
                         .format(levels))

    if len(level_definition) != 2:
        raise ValueError('Bad level definition {}. Correct format: '
                         '$(CMOR_TABLE)_$(COORDINATE_NAME)')

    coord = level_definition[1]
    if coord not in CMOR_TABLES[cmor_type].coords:
        raise ValueError('Coordinate {} not availabale for {}'
                         .format(coord, cmor_type))

    cmor = CMOR_TABLES[cmor_type].coords[coord]

    if len(cmor.requested) > 0:
        return [float(level) for level in cmor.requested]
    elif cmor.value:
        return [float(cmor.value)]
    else:
        raise ValueError('Coordinate {} in {} does not have requested values'
                         .format(coord, cmor_type))

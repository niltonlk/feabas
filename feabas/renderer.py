import matplotlib.tri
import numpy as np
import shapely
import shapely.geometry as shpgeo
from shapely.ops import unary_union

from feabas.constant import *
from feabas import spatial


class MeshRenderer:
    """
    A class to apply transforms according to a Mesh and render images.
    """
    def __init__(self, interpolators, **kwargs):
        self._interpolators = interpolators
        n_region = len(self._interpolators)
        self._offset = np.array(kwargs.get('offset', np.zeros((1,2))), copy=False).reshape(1,2)
        self._region_tree = kwargs.get('region_tree', None)
        self._weight_params = kwargs.get('weight_params', MESH_TRIFINDER_WHATEVER)
        self.weight_generator = kwargs.get('weight_generator', [None for _ in range(n_region)])
        self._collision_region = kwargs.get('collision_region', None)
        self._image_loader = kwargs.get('image_loader', None)
        self._resolution = kwargs.get('resolution', 4)


    @classmethod
    def from_mesh(cls, srcmesh, gear=(MESH_GEAR_MOVING, MESH_GEAR_INITIAL), **kwargs):
        include_flipped = kwargs.get('include_flipped', False)
        weight_params = kwargs.get('weight_params', MESH_TRIFINDER_INNERMOST)
        render_mask = srcmesh.triangle_mask_for_render()
        tri_info = srcmesh.tri_info(gear=gear[0], tri_mask=render_mask, include_flipped=include_flipped)
        offset0 = srcmesh.offset(gear=gear[0])
        region_tree = tri_info['region_tree']
        mattri_list = tri_info['matplotlib_tri']
        tidx_list = tri_info['triangle_index']
        vidx_list = tri_info['vertex_index']
        collisions = srcmesh.triangle_collisions(gear=gear[0], tri_mask=render_mask)
        if (collisions.size == 0) or (len(mattri_list) <= 1):
            weight_params = MESH_TRIFINDER_WHATEVER
        else:
            render_mask_indx = np.nonzero(render_mask)[0]
            collision_tidx = render_mask_indx[np.unique(collisions)]
        vertices_img = srcmesh.vertices_w_offset(gear=gear[-1])
        interpolators = []
        weight_generator = []
        collision_region = []
        for mattri, tidx, vidx, region in zip(mattri_list, tidx_list, vidx_list, region_tree.geometries):
            v0 = vertices_img[vidx]
            xinterp = matplotlib.tri.LinearTriInterpolator(mattri, v0[:,0])
            yinterp = matplotlib.tri.LinearTriInterpolator(mattri, v0[:,1])
            interpolators.append((xinterp, yinterp))
            if weight_params == MESH_TRIFINDER_WHATEVER:
                weight_generator.append(None)
            else:
                hitidx = np.intersect1d(tidx, collision_tidx)
                if hitidx.size == 0:
                    weight_generator.append(None)
                    continue
                mpl_tri, _, _ = srcmesh.mpl_tri(gear=gear[0], tri_mask=hitidx)
                collision_region.append(srcmesh.shapely_regions(gear=gear[0], tri_mask=hitidx))
                if weight_params == MESH_TRIFINDER_INNERMOST:
                    cx = mpl_tri.x
                    cy = mpl_tri.y
                    mpts = list(shpgeo.MultiPoint(np.stack((cx, cy), axis=-1)).geoms)
                    dis0 = region.boundary.distance(mpts) + EPSILON0
                    inside = region.intersects(mpts)
                    dis0[~inside] *= -1
                    weight_generator.append(matplotlib.tri.LinearTriInterpolator(mpl_tri, dis0))
                elif weight_params == MESH_TRIFINDER_LEAST_DEFORM:
                    deform = srcmesh.triangle_tform_deform(gear=gear[::-1], tri_mask=hitidx)
                    wt = np.exp(-2 * deform**2)
                    weight_generator.append((mpl_tri.get_trifinder(), wt))
                else:
                    raise ValueError
        if len(collision_region) > 0:
            collision_region = unary_union(collision_region)
        else:
            collision_region = None
        resolution = srcmesh.resolution
        return cls(interpolators, offset=offset0, region_tree=region_tree, weight_params=weight_params,
            weight_generator=weight_generator, collision_region=collision_region, resolution=resolution)


    def link_image_loader(self, imgloader):
        self._image_loader = imgloader


    def remap_subregions(self, field, **kwargs):
        pass


    def region_finder_for_points(self, xy, offsetting=True):
        """
        find the regions a collection of points belong to.
        Args:
            xy (N x 2 ndarray): the querry points
        Return:
            rid (N ndarray): the region ids
        """
        xy0 = np.array(xy, copy=False).reshape(-1,2)
        if offsetting:
            xy0 -= self._offset
        if len(self._interpolators) == 1:
            return np.zeros(xy0.shape[0], dtype=np.int32)
        else:
            rids_out = np.full(xy0.shape[0], -1, dtype=np.int32)
        mpts = shpgeo.MultiPoint(xy0)
        pts_list = list(mpts.geoms)
        hits = self._region_tree.query(pts_list, predicate='intersects')
        if hits.size == 0:
            return rids_out
        uhits, uidx, cnts = np.unique(hits[0], return_index=True, return_counts=True)
        conflict = np.any(cnts > 1)
        if conflict:
            if self._weight_params in (MESH_TRIFINDER_INNERMOST, MESH_TRIFINDER_LEAST_DEFORM):
                conflict_pts_indices = uhits[cnts > 1]
                for pt_idx in conflict_pts_indices:
                    pmask = hits[0] == pt_idx
                    r_indices = hits[1, pmask]
                    mndis = -np.inf
                    r_sel = r_indices[0]
                    pxy = xy0[pt_idx].ravel()
                    for r_id in r_indices:
                        wg = self.weight_generator[r_id]
                        if wg is None:
                            continue
                        if self._weight_params == MESH_TRIFINDER_INNERMOST:
                            dis_m = wg(pxy[0], pxy[1])
                            if dis_m.mask:
                                continue
                            dis = dis_m.data
                        else:
                            trfd, wt0 = wg
                            tid = trfd(pxy[0], pxy[1])
                            if tid < 0:
                                continue
                            dis = wt0[tid]
                        if dis > mndis:
                            r_sel = r_id
                            mndis = dis
                    hits[1, pmask] = r_sel
            hits = hits[:, uidx]
        rids_out[hits[0]] = hits[1]
        return rids_out


    def region_finder_for_bbox(self, bbox, offsetting=True):
        """
        find the regions that intersect a bounding box.
        Args:
            bbox [xmin, ymin, xmax, ymax]: the bonding box
        Return:
            rid (N ndarray): the region ids
        """
        bbox0 = np.array(bbox).reshape(4)
        if len(self._interpolators) == 1:
            hits = 0
        else:
            if offsetting:
                bbox0 -= np.tile(self._offset.ravel(), 2)
            rect = shpgeo.box(*bbox0)
            hits = self._region_tree.query(rect, predicate='intersects')
        return np.atleast_1d(hits)


    def bbox_hit_collision(self, bbox, offsetting=True):
        """
        test if a bounding box hit the collision regions of the mesh.
        """
        if (self._collision_region is None) or (hasattr(self._collision_region, 'area') and (self._collision_region.area == 0)):
            return False
        bbox0 = np.array(bbox).reshape(4)
        if offsetting:
            bbox0 -= np.tile(self._offset.ravel(), 2)
        rect = shpgeo.box(*bbox0)
        return rect.intersects(self._collision_region)


    def field_w_weight(self, bbox, **kwargs):
        """
        compute the deformation field and their weight given a bounding box.
        Args:
            bbox: bounding box (xmin, xmax, ymin, ymax) in the output space,
                left/top included & right/bottom excluded
        Kwargs:
            region_id: which region to use when selecting the interpolator. If
                set to None, find the region that encloses the center of the
                bounding box
            compute_wt: whether to compute weight. If False, return the mask of
                the interpolated results as weight.
            out_resolution: output resolution. If set to None, assume the output
                resolution is the same as the intrinsic renderer resoluton.
        Return:
            x-field (ndarray): deformation field in x direction. None if
                bounding box not intersecting the interpolator.
            y-field (ndarray): deformation field in y direction. None if
                bounding box not intersecting the interpolator.
            weight (ndarray): the weight of the output field. None if bounding
                box not intersecting the interpolator.
        """
        region_id = kwargs.get('region_id', None)
        compute_wt =  kwargs.get('compute_wt', True)
        out_resolution = kwargs.get('out_resolution', None)
        offsetting = kwargs.get('offsetting', True)
        bbox0 = np.array(bbox, copy=False).reshape(4)
        if offsetting:
            bbox0 = bbox0 - np.tile(self._offset.ravel(), 2)
        if region_id is None:
            bcntr = ((bbox0[0] + bbox0[2] - 1)/2, (bbox0[1] + bbox0[3] - 1)/2)
            region_id = self.region_finder_for_points(bcntr, offsetting=False).item()
        if region_id == -1:
            return None, None, None
        interpX, interpY = self._interpolators[region_id]
        xs = np.linspace(bbox0[0], bbox0[2], num=bbox0[2]-bbox0[0], endpoint=False, dtype=float)
        ys = np.linspace(bbox0[1], bbox0[3], num=bbox0[3]-bbox0[1], endpoint=False, dtype=float)
        if out_resolution is not None:
            scale = out_resolution / self._resolution
            xs = spatial.scale_coordinates(xs, scale)
            ys = spatial.scale_coordinates(ys, scale)
        xx, yy = np.meshgrid(xs, ys)
        map_x = interpX(xx, yy)
        map_y = interpY(xx, yy)
        mask = map_x.mask | map_y.mask
        if np.all(mask, axis=None):
            return None, None, None
        x_field = np.nan_to_num(map_x.data, copy=False)
        y_field = np.nan_to_num(map_y.data, copy=False)
        weight = 1 - mask.astype(np.float32)
        weight_generator = self.weight_generator[region_id]
        if compute_wt and (weight_generator is not None):
            if self._weight_params == MESH_TRIFINDER_INNERMOST:
                wt = weight_generator(xx, yy)
                if not np.all(wt.mask, axis=None):
                    wtmx = wt.max()
                    weight = weight * np.nan_to_num(wt.data, copy=False, nan=wtmx)
            elif self._weight_params == MESH_TRIFINDER_LEAST_DEFORM:
                trfd, wt0 = weight_generator
                tid = trfd(xx, yy)
                omask = tid < 0
                if not np.all(tid < 0, axis=None):
                    wt = wt0[tid]
                    wt[omask] = 1
                    weight = weight * wt
        return x_field, y_field, weight


    def local_affine_tform(self, pt, offsetting=True, svd_clip=None):
        pt0 = np.array(pt, copy=False).ravel()
        if offsetting:
            pt0 = pt0 - self._offset.ravel()
        region_id = self.region_finder_for_points(pt0, offsetting=False).item()
        interpX, interpY = self._interpolators[region_id]
        axx, axy = interpX.gradient(pt0[0], pt0[1])
        if axx.mask:
            return None, None
        ayx, ayy = interpY.gradient(pt0[0], pt0[1])
        A = np.array([[axx.data, ayx.data], [axy.data, ayy.data]])
        if svd_clip is not None:
            u, s, vh = np.linalg.svd(A, compute_uv=True)
            if hasattr(svd_clip, '__len__'):
                s = s.clip(svd_clip[0], svd_clip[-1])
            else:
                s = s.clip(1/(1+svd_clip), 1+svd_clip)
            A = u @ np.diag(s) @ vh
        pt1_x = interpX(pt0[0], pt0[1])
        pt1_y = interpY(pt0[0], pt0[1])
        pt1 = np.array([pt1_x.data, pt1_y.data])
        t = pt1 - pt0 @ A
        return A, t


    def crop_field(self, bbox, **kwargs):
        """
        compute the deformation field within a bounding box.
        Args:
            bbox: bounding box (xmin, xmax, ymin, ymax) in the output space,
                left/top included & right/bottom excluded
        Kwargs:
            out_resolution: output resolution. If set to None, assume the output
                resolution is the same as the intrinsic renderer resoluton.
        Return:
            x-field (ndarray): deformation field in x direction. None if
                bounding box not intersecting the interpolator.
            y-field (ndarray): deformation field in y direction. None if
                bounding box not intersecting the interpolator.
            mask (ndarray): region of invalid pixels.
        """
        mode = kwargs.get('mode', RENDER_FULL)
        offsetting = kwargs.get('offsetting', True)
        out_resolution = kwargs.get('out_resolution', None)
        bbox0 = np.array(bbox).reshape(4)
        if offsetting:
            bbox0 = bbox0 - np.tile(self._offset.ravel(), 2)
        if mode in (RENDER_LOCAL_RIGID, RENDER_LOCAL_AFFINE):
            bcntr = ((bbox0[0] + bbox0[2] - 1)/2, (bbox0[1] + bbox0[3] - 1)/2)
            if mode == RENDER_LOCAL_RIGID:
                svd_clip = 0
            else:
                svd_clip = kwargs.get('svd_clip', None)
            A, t = self.local_affine_tform(bcntr, offsetting=False, svd_clip=svd_clip)
            if A is None:
                return None, None, None
            xs = np.linspace(bbox0[0], bbox0[2], num=bbox0[2]-bbox0[0], endpoint=False, dtype=float)
            ys = np.linspace(bbox0[1], bbox0[3], num=bbox0[3]-bbox0[1], endpoint=False, dtype=float)
            if out_resolution is not None:
                scale = out_resolution / self._resolution
                xs = spatial.scale_coordinates(xs, scale)
                ys = spatial.scale_coordinates(ys, scale)
            xx, yy = np.meshgrid(xs, ys)
            x_field = xx * A[0,0] + yy * A[1,0] + t[0]
            y_field = xx * A[0,1] + yy * A[1,1] + t[1]
            mask = np.zeros_like(x_field, dtype=bool)
        elif mode == RENDER_CONTIGEOUS:
            x_field, y_field, weight = self.field_w_weight(bbox0, region_id=None,
                out_resolution=out_resolution, offsetting=False, compute_wt=True)
            if weight is None:
                mask = None
            else:
                mask = weight <= 0
        elif mode == RENDER_FULL:
            blend = kwargs.get('blend', BLEND_LINEAR)
            regions = self.region_finder_for_bbox(bbox0, offsetting=False)
            if regions.size == 0:
                return None, None, None
            initialized = False
            for rid in regions:
                xf, yf, wt = self.field_w_weight(bbox0, region_id=rid,
                    out_resolution=out_resolution, offsetting=False, compute_wt=True)
                if xf is None:
                    continue
                if not initialized:
                    x_field = np.zeros_like(xf)
                    y_field = np.zeros_like(yf)
                    weight = np.zeros_like(wt)
                    initialized = True
                if blend == BLEND_LINEAR:
                    x_field = x_field + xf * wt
                    y_field = y_field + yf * wt
                    weight = weight + wt
                elif blend == BLEND_MAX:
                    tmask = wt >= weight
                    x_field[tmask] = xf[tmask]
                    y_field[tmask] = yf[tmask]
                    weight[tmask] = wt[tmask]
                else:
                    tmask = wt > 0
                    x_field[tmask] = xf[tmask]
                    y_field[tmask] = yf[tmask]
                    weight[tmask] = wt[tmask]
            if not initialized:
                return None, None, None
            if blend == BLEND_LINEAR:
                x_field = np.nan_to_num(x_field / weight, nan=0,posinf=0, neginf=0)
                y_field = np.nan_to_num(y_field / weight, nan=0,posinf=0, neginf=0)
            mask = weight <= 0
        else:
            raise ValueError
        return x_field, y_field, mask
        


    def crop(self, bbox, return_empty=False, **kwargs):
        mode = kwargs.get('mode', RENDER_FULL)
        image_loader = kwargs.get('image_loader', self._image_loader)
        if image_loader is None:
            raise RuntimeError('Image loader not defined.')
        pass


    @property
    def bounds(self):
        if not hasattr(self, '_bounds'):
            xmin, ymin, xmax, ymax = np.inf, np.inf, -np.inf, -np.inf
            for m in self._weight_params:
                xmin = min(xmin, np.min(m.x) + self._offset.ravel()[0])
                ymin = min(ymin, np.min(m.y) + self._offset.ravel()[1])
                xmax = max(xmax, np.max(m.x) + self._offset.ravel()[0])
                ymax = max(ymax, np.max(m.y) + self._offset.ravel()[1])
            self._bounds = (xmin, ymin, xmax, ymax)
        return self._bounds

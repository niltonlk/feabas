from collections import namedtuple
import cv2
import collections
import gc
import importlib
import os

import numpy as np
from scipy import sparse
from scipy.ndimage import gaussian_filter1d
import scipy.sparse.csgraph as csgraph

import feabas.constant as const


class Match:
    def __init__(self, xy0, xy1, weight, class_id0=None, class_id1=None, angle0=None, angle1=None):
        assert xy0.shape[0] == xy1.shape[0]
        self.xy0 = xy0
        self.xy1 = xy1
        self._weight = weight
        self._class_id0 = class_id0
        self._class_id1 = class_id1
        self._angle0 = angle0
        self._angle1 = angle1

    def copy(self):
        xy0 = self.xy0
        xy1 = self.xy1
        weight = self._weight
        class_id0 = self._class_id0
        class_id1 = self._class_id1
        angle0 = self._angle0
        angle1 = self._angle1
        return self.__class__(xy0, xy1, weight, class_id0=class_id0, class_id1=class_id1, angle0=angle0, angle1=angle1)

    @classmethod
    def from_keypoints(cls, kps0, kps1, weight=None):
        xy0 = kps0.xy + kps0.offset
        xy1 = kps1.xy + kps1.offset
        class_id0 = kps0._class_id
        class_id1 = kps1._class_id
        angle0 = kps0._angle
        angle1 = kps1._angle
        return cls(xy0, xy1, weight, class_id0=class_id0, class_id1=class_id1, angle0=angle0, angle1=angle1)

    def filter_match(self, indx, inplace=True):
        if inplace:
            mtch = self
        else:
            mtch = self.copy()
        if indx is None:
            return mtch
        mtch.xy0 = mtch.xy0[indx]
        mtch.xy1 = mtch.xy1[indx]
        if mtch._weight is not None:
            mtch._weight = mtch._weight[indx]
        if mtch._class_id0 is not None:
            mtch._class_id0 = mtch._class_id0[indx]
        if mtch._class_id1 is not None:
            mtch._class_id1 = mtch._class_id1[indx]
        if mtch._angle0 is not None:
            mtch._angle0 = mtch._angle0[indx]
        if mtch._angle1 is not None:
            mtch._angle1 = mtch._angle1[indx]
        return mtch

    def sort_match_by_weight(self):
        if self._weight is None:
            return self
        indx = np.argsort(self._weight, kind='stable')
        if not np.all(indx == np.arange(indx.size)):
            self.filter_match(indx[::-1])
        return self

    def reset_weight(self, val=None):
        if val is not None:
            self._weight = np.full(self.num_points, val, dtype=np.float32)
        else:
            self._weight = None

    @property
    def num_points(self):
        return self.xy0.shape[0]

    @property
    def weight(self):
        if self._weight is None:
            return np.ones(self.num_points, dtype=np.float32)
        else:
            return self._weight

    @property
    def class_id0(self):
        if self._class_id0 is None:
            return np.ones(self.num_points, dtype=np.int16)
        else:
            return self._class_id0
        
    @property
    def class_id1(self):
        if self._class_id1 is None:
            return np.ones(self.num_points, dtype=np.int16)
        else:
            return self._class_id1

    @property
    def angle0(self):
        if self._angle0 is None:
            return np.zeros(self.num_points, dtype=np.float32)
        else:
            return self._angle0

    @property
    def angle1(self):
        if self._angle1 is None:
            return np.zeros(self.num_points, dtype=np.float32)
        else:
            return self._angle1


def imread(path, **kwargs):
    flag = kwargs.get('flag', cv2.IMREAD_UNCHANGED)
    return cv2.imread(path, flag)


def imwrite(path, image):
    return cv2.imwrite(path, image)


def inverse_image(img, dtype=np.uint8):
    if dtype is None:
        if isinstance(img, np.ndarray):
            dtype = img.dtype
        else:
            dtype = type(img)
    dtype = np.dtype(dtype)
    if np.issubdtype(dtype, np.integer):
        intmx = np.iinfo(dtype).max
        return intmx - img
    elif np.issubdtype(dtype, np.floating):
        return -img
    else:
        raise TypeError(f'{dtype} not invertable.')


def z_order(indices, base=2):
    """
    generating z-order from multi-dimensional indices.
    Args:
        indices(Nxd ndarray): indexing arrays with each colume as a dimension.
            Integer entries assumed.
    Return:
        z-order(Nx1 ndarray): the indices that would sort the points in z-order.
    """
    ndim = indices.shape[-1]
    indices = indices - indices.min(axis=0)
    indices_casted = np.zeros_like(indices)
    pw = 0
    while np.any(indices > 0):
        mod = indices % base
        indices_casted = indices_casted + mod * (base ** (ndim * pw))
        indices = np.floor(indices / base)
        pw += 1
    z_order_score = np.sum(indices_casted * (base ** np.arange(ndim)), axis=-1)
    return np.argsort(z_order_score, kind='stable')


def render_by_subregions(map_x, map_y, mask, img_loader, fileid=None,  **kwargs):
    """
    break the render job to small regions in case the target source image is
    too large to fit in RAM.
    """
    rintp = kwargs.get('remap_interp', cv2.INTER_LANCZOS4)
    mx_dis = kwargs.get('mx_dis', 16300)
    fillval = kwargs.get('fillval', img_loader.default_fillval)
    dtype_out = kwargs.get('dtype_out', img_loader.dtype)
    return_empty = kwargs.get('return_empty', False)
    if map_x.size == 0:
        return None
    if not np.any(mask, axis=None):
        if return_empty:
            return np.full_like(map_x, fillval, dtype=dtype_out)
        else:
            return None
    imgt = np.full_like(map_x, fillval, dtype=dtype_out)
    to_render = mask
    multichannel = False
    while np.any(to_render, axis=None):
        indx0, indx1 = np.nonzero(to_render)
        indx0_sel = indx0[indx0.size//2]
        indx1_sel = indx1[indx1.size//2]
        xx0 = map_x[indx0_sel, indx1_sel]
        yy0 = map_y[indx0_sel, indx1_sel]
        mskt = (np.abs(map_x - xx0) < mx_dis) & (np.abs(map_y - yy0) < mx_dis) & to_render
        xmin = np.floor(map_x[mskt].min()) - 4 # Lanczos 8x8 kernel
        xmax = np.ceil(map_x[mskt].max()) + 4
        ymin = np.floor(map_y[mskt].min()) - 4
        ymax = np.ceil(map_y[mskt].max()) + 4
        bbox = (int(xmin), int(ymin), int(xmax), int(ymax))
        if fileid is None:
            img0 = img_loader.crop(bbox, **kwargs)
        else:
            img0 = img_loader.crop(bbox, fileid, **kwargs)
        if img0 is None:
            to_render = to_render & (~mskt)
            continue
        if (len(img0.shape) > 2) and (not multichannel):
            # multichannel
            num_channel = img0.shape[-1]
            imgt = np.stack((imgt, )*num_channel, axis=-1)
            multichannel = True
        cover_ratio = np.sum(mskt) / mskt.size
        if cover_ratio > 0.25:
            map_xt = map_x - xmin
            map_yt = map_y - ymin
            imgtt = cv2.remap(img0, map_xt.astype(np.float32), map_yt.astype(np.float32),
                interpolation=rintp, borderMode=cv2.BORDER_CONSTANT, borderValue=fillval)
            if multichannel:
                mskt3 = np.stack((mskt, )*imgtt.shape[-1], axis=-1)
                imgt[mskt3] = imgtt[mskt3]
            else:
                imgt[mskt] = imgtt[mskt]
        else:
            map_xt = map_x[mskt] - xmin
            map_yt = map_y[mskt] - ymin
            N_pad = int(np.ceil((map_xt.size)**0.5))
            map_xt_pad = np.pad(map_xt, (0, N_pad**2 - map_xt.size)).reshape(N_pad, N_pad)
            map_yt_pad = np.pad(map_yt, (0, N_pad**2 - map_yt.size)).reshape(N_pad, N_pad)
            imgt_pad = cv2.remap(img0, map_xt_pad.astype(np.float32), map_yt_pad.astype(np.float32),
                interpolation=rintp, borderMode=cv2.BORDER_CONSTANT, borderValue=fillval)
            if multichannel:
                imgtt = imgt_pad.reshape(-1, num_channel)
                imgtt = imgtt[:(map_xt.size), :]
                mskt3 = np.stack((mskt, )*imgtt.shape[-1], axis=-1)
                imgt[mskt3] = imgtt.ravel()
            else:
                imgtt = imgt_pad.ravel()
                imgtt = imgtt[:(map_xt.size)]
                imgt[mskt] = imgtt.ravel()
        to_render = to_render & (~mskt)
    return imgt


def masked_dog_filter(img, sigma, mask=None):
    """
    apply Difference of Gaussian filter to an image. if a mask is provided, make
    sure any signal outside the mask will not bleed out.
    Args:
        img (ndarray): C x H x W.
        sigma (float): standard deviation of first Gaussian kernel.
        mask: region that should be kept. H x W
    """
    sigma0, sigma1 = sigma, 2 * sigma
    if not np.issubdtype(img.dtype, np.floating):
        img = img.astype(np.float32)
    img0f = gaussian_filter1d(gaussian_filter1d(img, sigma0, axis=-1, mode='nearest'), sigma0, axis=-2, mode='nearest')
    img1f = gaussian_filter1d(gaussian_filter1d(img, sigma1, axis=-1, mode='nearest'), sigma1, axis=-2, mode='nearest')
    imgf = img0f - img1f
    if (mask is not None) and (not np.all(mask, axis=None)):
        mask_img = img.ptp() * (1 - mask)
        mask0f = gaussian_filter1d(gaussian_filter1d(mask_img, sigma0, axis=-1, mode='nearest'), sigma0, axis=-2, mode='nearest')
        mask1f = gaussian_filter1d(gaussian_filter1d(mask_img, sigma1, axis=-1, mode='nearest'), sigma1, axis=-2, mode='nearest')
        maskf = np.maximum(mask0f, mask1f)
        imgf_a = np.abs(imgf)
        imgf_a = (imgf_a - maskf).clip(0, None)
        imgf = imgf_a * np.sign(imgf)
    return imgf


def divide_bbox(bbox, **kwargs):
    xmin, ymin, xmax, ymax = bbox
    ht = ymax - ymin
    wd = xmax - xmin
    block_size = kwargs.get('block_size', max(ht, wd))
    min_num_blocks = kwargs.get('min_num_blocks', 1)
    round_output = kwargs.get('round_output', True)
    shrink_factor = kwargs.get('shrink_factor', 1)
    if not hasattr(block_size, '__len__'):
        block_size = (block_size, block_size)
    if not hasattr(min_num_blocks, '__len__'):
        min_num_blocks = (min_num_blocks, min_num_blocks)
    Nx = max(np.ceil(wd / block_size[1]), min_num_blocks[1])
    Ny = max(np.ceil(ht / block_size[0]), min_num_blocks[0])
    dx = int(np.ceil(wd / Nx))
    dy = int(np.ceil(ht / Ny))
    xt = np.linspace(xmin, xmax-dx, num=int(Nx), endpoint=True)
    yt = np.linspace(ymin, ymax-dy, num=int(Ny), endpoint=True)
    if shrink_factor != 1:
        dx_new = dx * shrink_factor
        dy_new = dy * shrink_factor
        xt = xt + (dx - dx_new)/2
        yt = yt + (dy - dy_new)/2
        dx = int(np.ceil(dx_new))
        dy = int(np.ceil(dy_new))
    if round_output:
        xt = np.round(xt).astype(np.int32)
        yt = np.round(yt).astype(np.int32)
    xx, yy = np.meshgrid(xt, yt)
    return xx.ravel(), yy.ravel(), xx.ravel() + dx, yy.ravel() + dy


def intersect_bbox(bbox0, bbox1):
    xmin = max(bbox0[0], bbox1[0])
    ymin = max(bbox0[1], bbox1[1])
    xmax = min(bbox0[2], bbox1[2])
    ymax = min(bbox0[3], bbox1[3])
    return (xmin, ymin, xmax, ymax), (xmin < xmax) and (ymin < ymax)


def find_elements_in_array(array, elements, tol=0):
    # if find elements in array, return indices, otherwise return -1
    shp = elements.shape
    array = array.ravel()
    elements = elements.ravel()
    sorter = array.argsort()
    idx = np.searchsorted(array, elements, sorter=sorter)
    idx = sorter[idx.clip(0, array.size-1)]
    neq = np.absolute(array[idx] - elements) > tol
    idx[neq] = -1
    return idx.reshape(shp)


def numpy_to_str_ascii(ar):
    t = ar.clip(0,255).astype(np.uint8).ravel()
    return t.tostring().decode('ascii')


def str_to_numpy_ascii(s):
    t =  np.frombuffer(s.encode('ascii'), dtype=np.uint8)
    return t


def load_plugin(plugin_name):
    modl, plugname = plugin_name.rsplit('.', 1)
    plugin_mdl = importlib.import_module(modl)
    plugin = getattr(plugin_mdl, plugname)
    return plugin


def hash_numpy_array(ar):
    if isinstance(ar, np.ndarray):
        return hash(ar.data.tobytes())
    elif isinstance(ar, list):
        return hash(tuple(ar))
    else:
        return hash(ar)


def indices_to_bool_mask(indx, size=None):
    if isinstance(indx, np.ndarray) and indx.dtype==bool:
        return indx
    if size is None:
        size = np.max(indx)
    mask = np.zeros(size, dtype=bool)
    mask[indx] = True
    return mask


def crop_image_from_bbox(img, bbox_img, bbox_out, **kwargs):
    """
    Crop an image based on the bounding box
    Args:
        img (np.ndarray): input image to be cropped.
        bbox_img: bounding box of the input image. [xmin, ymin, xmax, ymax]
        bbox_out: bounding box of the output image. [xmin, ymin, xmax, ymax]
    Kwargs:
        return_index (bool): if True, return the overlapping region of bbox_img
            and bbox_out & the slicings to position the overlapping region onto
            the output image; if False, return the output sized image without
            slicings.
        return_empty (bool): if False, return None if bbox_img and bbox_out not
            overlapping; if True, return an ndarray filled with fillval.
        fillval(scalar): fill values for invalid pixels in the output image.
    Return:
        imgout: output image. if return_indx is True, only return the overlap
            region between the two bboxes.
        index: the slicings to position the overlapping onto the output image.
            return only when return_index is True.
    """
    return_index = kwargs.get('return_index', False)
    return_empty = kwargs.get('return_empty', False)
    fillval = kwargs.get('fillval', 0)
    x0 = bbox_img[0]
    y0 = bbox_img[1]
    blkht = min(bbox_img[3] - bbox_img[1], img.shape[0])
    blkwd = min(bbox_img[2] - bbox_img[0], img.shape[1])
    outht = bbox_out[3] - bbox_out[1]
    outwd = bbox_out[2] - bbox_out[0]
    xmin = max(x0, bbox_out[0])
    xmax = min(x0 + blkwd, bbox_out[2])
    ymin = max(y0, bbox_out[1])
    ymax = min(y0 + blkht, bbox_out[3])
    if xmin >= xmax or ymin >= ymax:
        if return_index:
            return None, None
        else:
            if return_empty:
                outsz = [outht, outwd] + list(img.shape)[2:]
                imgout = np.full_like(img, fillval, shape=outsz)
                return imgout
            else:
                return None
    cropped = img[(ymin-y0):(ymax-y0), (xmin-x0):(xmax-x0), ...]
    dimpad = len(img.shape) - 2
    indx = tuple([slice(ymin-bbox_out[1], ymax-bbox_out[1]), slice(xmin-bbox_out[0],xmax-bbox_out[0])] +
            [slice(0, None)] * dimpad)
    if return_index:
        return cropped, indx
    else:
        outsz = [outht, outwd] + list(img.shape)[2:]
        imgout = np.full_like(img, fillval, shape=outsz)
        imgout[indx] = cropped
        return imgout


def chain_segment_rings(segments, directed=True, conn_lable=None) -> list:
    """
    Given id pairs of line segment points, assemble them into (closed) chains.
    Args:
        segments (Nsegx2 ndarray): vertices' ids of each segment. Each segment
            should only appear once, and the rings should be simple (no self
            intersection).
        directed (bool): whether the segments provided are directed. Default to
            True.
        conn_label (np.ndarray): preset groupings of the segments. If set to
            None, use the connected components from vertex adjacency.
    """
    inv_map, seg_n = np.unique(segments, return_inverse=True)
    seg_n = seg_n.reshape(segments.shape)
    if not directed:
        seg_n = np.sort(seg_n, axis=-1)
    Nseg = seg_n.shape[0]
    Npts = inv_map.size
    chains = []
    if conn_lable is None:
        A = sparse.csr_matrix((np.ones(Nseg), (seg_n[:,0], seg_n[:,1])), shape=(Npts, Npts))
        N_conn, V_conn = csgraph.connected_components(A, directed=directed, return_labels=True)
    else:
        u_lbl, S_conn = np.unique(conn_lable,  return_inverse=True)
        N_conn = u_lbl.size
        A = sparse.csc_matrix((S_conn+1, (seg_n[:,0], seg_n[:,1])), shape=(Npts, Npts))
    for n in range(N_conn):
        if conn_lable is None:
            vtx_mask = V_conn == n
            An = A[vtx_mask][:, vtx_mask]
        else:
            An0 = A == (n+1)
            An0.eliminate_zeros()
            vtx_mask = np.zeros(Npts, dtype=bool)
            sidx = np.unique(seg_n[S_conn == n], axis=None)
            vtx_mask[sidx] = True
            An = An0[vtx_mask][:, vtx_mask]
        vtx_idx = np.nonzero(vtx_mask)[0]
        while An.max() > 0:
            idx0, idx1 = np.unravel_index(np.argmax(An), An.shape)
            An[idx0, idx1] = 0
            An.eliminate_zeros()
            dis, pred = csgraph.shortest_path(An, directed=directed, return_predecessors=True, indices=idx1)
            if dis[idx0] < 0:
                raise ValueError('segment rings not closed.')
            seq = [idx0]
            crnt_node = idx0
            while True:
                crnt_node = pred[crnt_node]
                if crnt_node >= 0:
                    seq.insert(0, crnt_node)
                else:
                    break
            chain_idx = vtx_idx[seq]
            chains.append(inv_map[chain_idx])
            covered_edges = np.stack((seq[:-1], seq[1:]), axis=-1)
            if not directed:
                covered_edges = np.sort(covered_edges, axis=-1)
            R = sparse.csr_matrix((np.ones(len(seq)-1), (covered_edges[:,0], covered_edges[:,1])), shape=An.shape)
            An = An - R
    return chains


def signed_area(vertices, triangles) -> np.ndarray:
    tripts = vertices[triangles]
    v0 = tripts[:,1,:] - tripts[:,0,:]
    v1 = tripts[:,2,:] - tripts[:,1,:]
    return np.cross(v0, v1)


def expand_image(img, target_size, slices, fillval=0):
    if len(img.shape) == 3:
        target_size = list(target_size) + [img.shape[-1]]
    img_out = np.full_like(img, fillval, shape=target_size)
    img_out[slices[0], slices[1], ...] = img
    return img_out


def bbox_centers(bboxes):
    bboxes = np.array(bboxes, copy=False)
    cntr = 0.5 * bboxes @ np.array([[1,0],[0,1],[1,0],[0,1]]) - 0.5
    return cntr


def bbox_sizes(bboxes):
    bboxes = np.array(bboxes, copy=False)
    szs = bboxes @ np.array([[0,-1],[-1,0],[0,1],[1,0]])
    return szs.clip(0, None)


def bbox_intersections(bboxes0, bboxes1):
    xy_min = np.maximum(bboxes0[...,:2], bboxes1[...,:2])
    xy_max = np.minimum(bboxes0[...,-2:], bboxes1[...,-2:])
    bbox_int = np.concatenate((xy_min, xy_max), axis=-1)
    width = np.min(xy_max - xy_min, axis=-1)
    return bbox_int, width


def bbox_union(bboxes):
    bboxes = np.array(bboxes, copy=False)
    bboxes = bboxes.reshape(-1, 4)
    xy_min = bboxes[:,:2].min(axis=0)
    xy_max = bboxes[:,-2:].max(axis=0)
    return np.concatenate((xy_min, xy_max), axis=None)


def bbox_enlarge(bboxes, margin=0):
    return np.array(bboxes, copy=False) + np.array([-margin, -margin, margin, margin])


def parse_coordinate_files(filename, **kwargs):
    """
    parse a coordinate txt file. Each row in the file follows the pattern:
        image_path  x_min  y_min  x_max(optional)  y_max(optional)
    if x_max and y_max is not provided, they are inferred from tile_size.
    If relative path is provided in the image_path colume, at the first line
    of the file, the root_dir can be defined as:
        {ROOT_DIR}  rootdir_to_the_path
        {TILE_SIZE} tile_height tile_width
    Args:
        filename(str): full path to the coordinate file.
    Kwargs:
        rootdir: if the imgpaths colume in the file is relative paths, can
            use this to prepend the paths. Set to None to disable.
        tile_size: the tile size used to compute the bounding boxes in the
            absense of x_max and y_max in the file. If None, will read an
            image file to figure out
        delimiter: the delimiter to separate each colume in the file. If set
            to None, any whitespace will be considered.
    """
    root_dir = kwargs.get('root_dir', None)
    tile_size = kwargs.get('tile_size', None)
    delimiter = kwargs.get('delimiter', '\t') # None for any whitespace
    resolution = kwargs.get('resolution', const.DEFAULT_RESOLUTION)
    imgpaths = []
    bboxes = []
    with open(filename, 'r') as f:
        lines = f.readlines()
    if len(lines) == 0:
        raise RuntimeError(f'empty file: {filename}')
    start_line = 0
    for line in lines:
        if '{ROOT_DIR}' in line:
            start_line += 1
            tlist = line.strip().split(delimiter)
            if len(tlist) >= 2:
                root_dir = tlist[1]
        elif '{TILE_SIZE}' in line:
            start_line += 1
            tlist = line.strip().split(delimiter)
            if len(tlist) == 2:
                tile_size = (int(tlist[1]), int(tlist[1]))
            elif len(tlist) > 2:
                tile_size = (int(tlist[1]), int(tlist[2]))
            else:
                continue
        elif '{RESOLUTION}' in line:
            start_line += 1
            tlist = line.strip().split(delimiter)
            if len(tlist) >= 2:
                resolution = float(tlist[1])
        else:
            break
    relpath = bool(root_dir)
    for line in lines[start_line:]:
        line = line.strip()
        tlist = line.split(delimiter)
        if len(tlist) < 3:
            raise RuntimeError(f'corrupted coordinate file: {filename}')
        mpath = tlist[0]
        x_min = float(tlist[1])
        y_min = float(tlist[2])
        if (len(tlist) >= 5) and (tile_size is None):
            x_max = float(tlist[3])
            y_max = float(tlist[4])
        else:
            if tile_size is None:
                if relpath:
                    mpath_f = os.path.join(root_dir, mpath)
                else:
                    mpath_f = mpath
                img = imread(mpath_f, flag=cv2.IMREAD_GRAYSCALE)
                tile_size = img.shape
            x_max = x_min + tile_size[-1]
            y_max = y_min + tile_size[0]
        imgpaths.append(mpath)
        bboxes.append((x_min, y_min, x_max, y_max))
    return imgpaths, bboxes, root_dir, resolution


##--------------------------------- caches -----------------------------------##

class Node:
    """
    Node used in doubly linked list.
    """
    def __init__(self, key, data):
        self.key = key # harshable key for indexing
        self.data = data
        self.pointer = None  # store e.g. pointer to freq node
        self.prev = None
        self.next = None


    def modify_data(self, data):
        self.data = data



class DoublyLinkedList:
    """
    Doubly linked list for LFU cache etc.
    Args:
        item(tuple): (key, data) pair of the first node. Return empty list if
            set to None.
    """
    def __init__(self, item=None):
        if item is None:
            self.head = None
            self.tail = None
            self._number_of_nodes = 0
        else:
            if isinstance(item, Node):
                first_node = item
            else:
                first_node = Node(*item)
            self.head = first_node
            self.tail = first_node
            self._number_of_nodes = 1


    def __len__(self):
        return self._number_of_nodes


    def clear(self):
        # Traverse the list to break reference cycles
        while self.head is not None:
            self.remove_head()


    def insert_before(self, node, item):
        if isinstance(item, Node):
            new_node = item
        else:
            new_node = Node(*item)
        if node is None:
            # empty list
            self.head = new_node
            self.tail = new_node
        else:
            prevnode = node.prev
            new_node.prev = prevnode
            new_node.next = node
            node.prev = new_node
            if prevnode is None:
                self.head = new_node
            else:
                prevnode.next = new_node
        self._number_of_nodes += 1


    def insert_after(self, node, item):
        if isinstance(item, Node):
            new_node = item
        else:
            new_node = Node(*item)
        if node is None:
            # empty list
            self.head = new_node
            self.tail = new_node
        else:
            nextnode = node.next
            new_node.next = nextnode
            new_node.prev = node
            node.next = new_node
            if nextnode is None:
                self.tail = new_node
            else:
                nextnode.prev = new_node
        self._number_of_nodes += 1


    def pop_node(self, node):
        if node is None:
            return None
        prevnode = node.prev
        nextnode = node.next
        if prevnode is not None:
            prevnode.next = nextnode
        else:
            self.head = nextnode
        if nextnode is not None:
            nextnode.prev = prevnode
        else:
            self.tail = prevnode
        node.prev = None
        node.next = None
        self._number_of_nodes -= 1
        return node


    def remove_node(self, node):
        del node.key
        del node.data
        del node.pointer
        self.pop_node(node)


    def insert_head(self, item):
        self.insert_before(self.head, item)


    def insert_tail(self, item):
        self.insert_after(self.tail, item)


    def pop_head(self):
        return self.pop_node(self.head)


    def pop_tail(self):
        return self.pop_node(self.tail)


    def remove_head(self):
        self.remove_node(self.head)


    def remove_tail(self):
        self.remove_node(self.tail)



class CacheNull:
    """
    Cache class with no capacity. Mostlys to define Cache APIs.
    Attributes:
        _maxlen: the maximum capacity of the cache. No upper limit if set to None.
    """
    def __init__(self, maxlen=0):
        self._maxlen = maxlen

    def clear(self, instant_gc=False):
        """Clear cache"""
        if instant_gc:
            gc.collect()

    def item_accessed(self, key_list):
        """Add accessed time by 1 for items in key list (used for freq record)"""
        pass

    def __contains__(self, key):
        """Check item availability in the cache"""
        return False

    def __getitem__(self, key):
        """Access an item"""
        errmsg = "fail to access data from empty cache"
        raise NotImplementedError(errmsg)

    def __len__(self):
        """Current number of items in the cache"""
        return 0

    def __setitem__(self, key, data):
        """Cache an item"""
        pass

    def __iter__(self):
        """return the iterator"""
        yield from ()

    def update_item(self, key, data):
        """force update a cached item"""
        pass

    def _evict_item_by_key(self, key):
        """remove an item by providing the key"""
        pass


class CacheFIFO(CacheNull):
    """
    Cache with first in first out (FIFO) replacement policy.
    """
    def __init__(self, maxlen=None):
        self._maxlen = maxlen
        self._keys = collections.deque(maxlen=maxlen)
        self._vals = collections.deque(maxlen=maxlen)


    def clear(self, instant_gc=False):
        self._keys.clear()
        self._vals.clear()
        if instant_gc:
            gc.collect()


    def __contains__(self, key):
        return key in self._keys


    def __getitem__(self, key):
        if key in self._keys:
            indx = self._keys.index(key)
            return self._vals[indx]
        else:
            errmsg = "fail to access data with key {} from cached.".format(key)
            raise KeyError(errmsg)


    def __len__(self):
        return len(self._keys)


    def __setitem__(self, key, data):
        if (self._maxlen) == 0 or (key in self._keys):
            return
        self._keys.append(key)
        self._vals.append(data)


    def __iter__(self):
        for key in self._keys:
            yield key


    def update_item(self, key, data):
        if (self._maxlen) == 0:
            return
        if key in self._keys:
            indx = self._keys.index(key)
            self._vals[indx] = data
        else:
            self.__setitem__(key, data)


    def _evict_item_by_key(self, key):
        """remove an item from dequeue may be anti-pattern. set it to None"""
        if (self._maxlen) == 0:
            return
        if key in self._keys:
            indx = self._keys.index(key)
            self._vals[indx] = None



class CacheLRU(CacheNull):
    """
    Cache with least recently used (LRU) replacement policy
    """
    def __init__(self, maxlen=None):
        self._maxlen = maxlen
        self._cached_nodes = {}
        self._cache_list = DoublyLinkedList() # head:old <-> tail:new


    def clear(self, instant_gc=False):
        self._cached_nodes.clear()
        self._cache_list.clear()
        if instant_gc:
            gc.collect()


    def item_accessed(self, key_list):
        for key in key_list:
            self._move_item_to_tail(key)


    def _evict_item_by_key(self, key):
        if key in self._cached_nodes:
            node = self._cached_nodes.pop(key)
            self._cache_list.remove_node(node)


    def _evict_item_by_policy(self):
        node = self._cache_list.head
        if node is not None:
            key = node.key
            self._evict_item_by_key(key)


    def _move_item_to_tail(self, key):
        if key in self._cached_nodes:
            node = self._cached_nodes[key]
            if node.next is None:
                return
            node = self._cache_list.pop_node(node)
            self._cache_list.insert_tail(node)


    def __contains__(self, key):
        return key in self._cached_nodes


    def __getitem__(self, key):
        if key in self._cached_nodes:
            node = self._cached_nodes[key]
            self._move_item_to_tail(key)
            return node.data
        else:
            errmsg = "fail to access data with key {} from cached.".format(key)
            raise KeyError(errmsg)


    def __len__(self):
        return len(self._cached_nodes)


    def __setitem__(self, key, data):
        if (self._maxlen == 0) or (key in self._cached_nodes):
            return
        if self._maxlen is not None:
            while len(self._cached_nodes) >= self._maxlen:
                self._evict_item_by_policy()
        data_node = Node(key, data)
        self._cache_list.insert_tail(data_node)
        self._cached_nodes[key] = data_node


    def __iter__(self):
        for key in self._cached_nodes:
            yield key


    def update_item(self, key, data):
        if self._maxlen == 0:
            return
        if key in self._cached_nodes:
            data_node = self._cached_nodes[key]
            data_node.modify_data(data)
        else:
            self.__setitem__(key, data)



class CacheLFU(CacheNull):
    """
    Cache with least frequent used (LFU) replacement policy.
    Attributes:
        _cached_nodes(dict): dictionary holding the data nodes.
        _freq_list(DoublyLinkedList): frequecy list, with each node holding
            accessed frequency and pointing to a DoublyLinkedList holding
            cached data nodes, with later added nodes attached to the tail. Each
            data node contains cached data and points to its frequency node.
    """
    def __init__(self, maxlen=None):
        self._maxlen = maxlen
        self._cached_nodes = {}
        self._freq_list = DoublyLinkedList()


    def clear(self, instant_gc=False):
        for key in self._cached_nodes:
            self._evict_item_by_key(key)
        self._freq_list.clear()
        if instant_gc:
            gc.collect()


    def item_accessed(self, key_list):
        for key in key_list:
            self._increase_item_access_number_by_one(key)


    def _evict_item_by_key(self, key):
        if key in self._cached_nodes:
            node = self._cached_nodes.pop(key)
            freq_node = node.pointer
            cache_list = freq_node.pointer
            cache_list.remove_node(node)
            if (len(cache_list) == 0) and (freq_node.data != 0):
                self._freq_list.remove_node(freq_node)


    def _evict_item_by_policy(self):
        freq_node = self._freq_list.head
        while freq_node is not None:
            cache_list = freq_node.pointer
            if len(cache_list) > 0:
                key = cache_list.head.key
                self._evict_item_by_key(key)
                break
            else:
                freq_node = freq_node.next


    def _increase_item_access_number_by_one(self, key):
        if key in self._cached_nodes:
            node = self._cached_nodes[key]
            freq_node = node.pointer
            cache_list = freq_node.pointer
            cnt = freq_node.data
            if (freq_node.next is None) or (freq_node.next.data != cnt + 1):
                if len(cache_list) == 1:
                    # only this data node linked to the freq node.
                    freq_node.data += 1
                    return
                else:
                    self._freq_list.insert_after(freq_node, (None, cnt+1))
                    freq_node.next.pointer = DoublyLinkedList()
            target_cache_list = freq_node.next.pointer
            node = cache_list.pop_node(node)
            node.pointer = freq_node.next
            if (len(cache_list) == 0) and (freq_node.data != 0):
                self._freq_list.remove_node(freq_node)
            target_cache_list.insert_tail(node)


    def __contains__(self, key):
        return key in self._cached_nodes


    def __getitem__(self, key):
        if key in self._cached_nodes:
            node = self._cached_nodes[key]
            self._increase_item_access_number_by_one(key)
            return node.data
        else:
            errmsg = "fail to access data with key {} from cached.".format(key)
            raise KeyError(errmsg)

    def __len__(self):
        return len(self._cached_nodes)


    def __setitem__(self, key, data):
        if (self._maxlen == 0) or (key in self._cached_nodes):
            return
        if self._maxlen is not None:
            while len(self._cached_nodes) >= self._maxlen:
                self._evict_item_by_policy()
        if (self._freq_list.head is None) or (self._freq_list.head.data != 0):
            self._freq_list.insert_head((None, 0))
            self._freq_list.head.pointer = DoublyLinkedList()
        data_node = Node(key, data)
        freq_node = self._freq_list.head
        data_node.pointer = freq_node
        cache_list = freq_node.pointer
        cache_list.insert_tail(data_node)
        self._cached_nodes[key] = data_node


    def __iter__(self):
        for key in self._cached_nodes:
            yield key


    def update_item(self, key, data):
        if self._maxlen == 0:
            return
        if key in self._cached_nodes:
            data_node = self._cached_nodes[key]
            data_node.modify_data(data)
        else:
            self.__setitem__(key, data)


class CacheMFU(CacheLFU):
    """
    Cache with most frequent used replacement policy.
    This policy could be useful in applications like rendering when the purpose
    is to cover the entire dataset once, and data already accessed multiple times
    is less likely to be accessed again.
    """
    def _evict_item_by_policy(self):
        freq_node = self._freq_list.tail
        while freq_node is not None:
            cache_list = freq_node.pointer
            if len(cache_list) > 0:
                key = cache_list.head.key
                self._evict_item_by_key(key)
                break
            else:
                freq_node = freq_node.prev


def generate_cache(cache_type='fifo', maxlen=None):
    if (maxlen == 0) or (cache_type.lower() == 'none'):
        return CacheNull()
    elif cache_type.lower() == 'fifo':
        return CacheFIFO(maxlen=maxlen)
    elif cache_type.lower() == 'lru':
        return CacheLRU(maxlen=maxlen)
    elif cache_type.lower() == 'lfu':
        return CacheLFU(maxlen=maxlen)
    elif cache_type.lower() == 'mfu':
        return CacheMFU(maxlen=maxlen)
    else:
        errmsg = 'cache type {} not implemented'.format(cache_type)
        raise NotImplementedError(errmsg)

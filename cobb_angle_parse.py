import numpy as np
import os.path as path
import cv2
import folders as f
import argparse
import redundant_bones_filter as rbf

def cvshow(img):
    assert len(img.shape)==2
    #img = cv2.resize(img, dsize=None, fx=4, fy=4, interpolation=cv2.INTER_NEAREST)
    cv2.imshow("img", img)
    cv2.waitKey(0)
    cv2.destroyAllWindows()

def cvsave(img, name):
    assert len(img.shape)==2
    cv2.imwrite(path.join(f.validation_plot_out,"{}.jpg".format(name)), img)

def centeroid(heat, gaussian_thresh = 0.5):
    # Parse center point of connected components
    # Return [p][xy]
    ret, heat = cv2.threshold(heat, gaussian_thresh, 1., cv2.THRESH_BINARY)
    heat = np.array(heat * 255., np.uint8)
    # num: point number + 1 background
    num, labels = cv2.connectedComponents(heat)
    coords = []
    for label in range(1, num):
        mask = np.zeros_like(labels, dtype=np.uint8)
        mask[labels == label] = 255
        M = cv2.moments(mask)
        cX = int(M["m10"] / M["m00"])
        cY = int(M["m01"] / M["m00"])
        coords.append([cX, cY])
    return coords


def line_mask(pt1, pt2_list, hw):
    # Return images with a line from pt1 to each pts in pt2_list
    # Return image pixel value range: [0,1], nparray.
    assert len(hw) == 2
    zeros = np.zeros([len(pt2_list), hw[0], hw[1]], dtype=np.uint8)
    masks_with_line = [cv2.line(zeros[i_pt2], tuple(pt1), tuple(pt2), 255) for i_pt2, pt2 in enumerate(pt2_list)]
    masks_01 = np.array(masks_with_line, dtype=np.float32) / 255.

    return masks_01

def line_dist(pt1, pt2_list):
    # Return distances of a point to a list of points.
    # Return numpy array
    pt1 = np.array(pt1)
    pt2_list = np.array(pt2_list)
    dist_1d = pt2_list-pt1
    dist_2d = np.linalg.norm(dist_1d, axis=-1)
    return dist_2d

def coincidence_rate(paf, line_masks, distances):
    # Return confidences of a point connects to a list of points
    # Return nparray, range from 0 to around 5 (not 0-1 due to opencv line divides distance not equal to 1.0)
    assert len(paf.shape)==2
    assert len(line_masks.shape)==3
    assert len(distances.shape)==1
    coincidence = line_masks * paf  # [p2_len][h][w]
    co_sum = np.sum(coincidence, axis=(1,2))
    co_rate = co_sum / distances
    return co_rate

def center_coords(lcrc_pcm):
    # Return left center coordinates, right center coordinates.
    assert len(lcrc_pcm.shape)==3, "expected shape: (lr, h, w)"
    assert lcrc_pcm.shape[0]==2, "1st dim of pcm should have 2 elements: l and r"
    lcrc_coord = [centeroid(c) for c in lcrc_pcm[:]]  # lc_coord: [p][xy]
    return lcrc_coord

def coincidence_rate_from_pcm_paf(lcrc_coord, hw, paf):
    # Return confidences nparray with shape: [p1_len][p2_len]
    assert len(np.array(lcrc_coord[0]).shape)==2, "expected shape: (p, xy). length of lc, rc list can be different"
    assert len(hw)==2, "expected shape length: 2 for h and w"
    assert len(paf.shape)==2, "paf shape length should be 2"
    lc_coord, rc_coord = lcrc_coord
    coins = []  # coincidence rate list, shape: [pt1_len][pt2_len]
    for lc_pt in lc_coord[:]:
        p1_masks = line_mask(lc_pt, rc_coord, hw)  #[p2_len][h][w]
        p1_dist = line_dist(lc_pt, rc_coord)
        coin = coincidence_rate(paf, p1_masks,p1_dist)
        coins.append(coin)
    return np.array(coins)


def pairs_with_highest_confidence(coincidence_rate, confidence_lowerbound):
    # Return: 2 lists contains paired points. e.g.[3,4,5] and [3,4,6] means l3->r3, l4->r4, l6->r6
    pair_l, pair_r = [], []
    args_1d = np.argsort(coincidence_rate, axis=None)
    lc_args, rc_args = np.unravel_index(args_1d, coincidence_rate.shape)
    for i_arg in reversed(
            range(len(lc_args))):  # reverse: default argsort gives min->max sort, we want max->min results
        al = lc_args[i_arg]  # index of left center list
        ar = rc_args[i_arg]  # index of right center list

        if (al not in pair_l) and (ar not in pair_r):  # Best pair among all

            # Check if confidence too low (e.g. 2 wrong points at top and bottom).
            # Real pair should have cofidence of around 4.5
            if coincidence_rate[al][ar] > confidence_lowerbound:
                pair_l.append(al)
                pair_r.append(ar)
        else:
            # At least one point already had a better pair.
            pass
    assert len(pair_l) == len(pair_r)
    return (pair_l, pair_r)


def pair_args_to_value(pair_lr_args, lr_coords):
    # Convert pairs of xy args to pairs of xy values
    al, ar = np.array(pair_lr_args, dtype=np.int)[:]
    cl, cr = lr_coords
    # There may be single points with out pair. In that case , lengthes are different, lr_coords can't be converted to a numpy array,
    # cause vanilla python list error: "only integer scalar arrays can be converted to a scalar index".
    cl, cr = list(map(np.array, [cl, cr]))

    xy_l = cl[al]
    xy_r = cr[ar]
    return xy_l, xy_r

def bone_vectors(pair_lr_value):
    # Return vector of bones (for angle computation)
    # Shape [bone][xy]
    pair_lr_value = np.array(pair_lr_value)
    assert len(pair_lr_value.shape)==3, "shape should be:(lr, bones, xy)"
    assert pair_lr_value.shape[0]==2, "length of first dim should be 2 for l and r"
    l, r = pair_lr_value
    return r-l


def cos_angle(v1, v2):
    assert v1.shape == (2,)
    assert v2.shape == (2,)
    dot = np.dot(v1, v2)
    len1, len2 = list(map(np.linalg.norm, [v1, v2]))
    an_cos = dot / (len1 * len2)

    an_cos = an_cos.clip(-1., 1.)
    return an_cos

def angle_matrix(bone_vectors):
    # Return angle matrix: A[i][j]
    # Return degree of each 2 vectors, shape: [bone1][bone2]
    bone_vectors = np.array(bone_vectors)
    assert len(bone_vectors.shape)==2, "expected shape: (bone, xy)"

    num_bones = bone_vectors.shape[0]
    an_matrix = np.zeros((num_bones, num_bones))
    for i in range(num_bones):
        for j in range(num_bones):
            v1, v2=bone_vectors[i], bone_vectors[j]
            an_cos = cos_angle(v1, v2)
            an_matrix[i, j] = an_cos
    # cos_angle some times larger than 1 due to numerical precision
    an_matrix = np.clip(an_matrix, a_min=-1., a_max=1.)
    an_matrix = np.arccos(an_matrix)
    an_matrix = np.rad2deg(an_matrix)
    return an_matrix

def draw_pairs(lr_values,heat_hw, img):
    # Draw the line between pairs on image
    assert len(np.asarray(lr_values).shape)==3, "shape: (lr, p, xy)"
    assert len(img.shape)==2, "shape: (h,w)"
    lv, rv = lr_values
    draw_layer = np.zeros(heat_hw, dtype=np.uint8)
    for i in range(len(lv)):
        pt1 = lv[i]
        pt2 = rv[i]
        cv2.line(draw_layer, tuple(pt1), tuple(pt2), 255, 5)
        cv2.putText(draw_layer, str(i), tuple(pt2), cv2.FONT_HERSHEY_SIMPLEX, 1, 255)
    draw_layer = cv2.resize(draw_layer, tuple(reversed(img.shape)), interpolation=cv2.INTER_NEAREST)
    img = np.maximum(img, draw_layer)
    return img

def make_ind1_upper(ind1, ind2, pair_lr_value):
    # Check if ind1 is upper bone. If not, exchange ind1 and 2
    pair_lr_value = np.array(pair_lr_value)
    assert len(pair_lr_value.shape) == 3
    hmids = (pair_lr_value[0] + pair_lr_value[1]) / 2  # Horizontal mid points, [p][xy]
    hmids_y = hmids[:, 1]  # [p]
    mid1 = hmids_y[ind1]  #  Relies on leftpoint, y coord
    mid2 = hmids_y[ind2]
    if mid2 > mid1:  # ind2 is lower
        pass
    else:  # ind1 is lower
        temp = ind1
        ind1 = ind2
        ind2 = temp
    return ind1, ind2

def sort_pairs_by_y(pair_lr_value):
    # pairs was originally sorted by confidence, reorder them to sort by y value
    pair_lr_value = np.array(pair_lr_value)
    assert len(pair_lr_value.shape) == 3
    hmids = (pair_lr_value[0] + pair_lr_value[1]) / 2  # Horizontal mid points, [p][xy]
    hmids_y = hmids[:, 1]  # [p]
    order_y = np.argsort(hmids_y, axis=None)  # Index of confidence array, shows if array was sorted by y.
    pair_lr_value = pair_lr_value[:, order_y, :]
    return pair_lr_value


def max_angle_indices(bones, pair_lr_value):
    # 2 indices which compose the largest angle. ind1 >= ind2
    assert len(bones.shape) == 2, "expect [b][xy]"
    # [len_b][len_b] angle matrix
    am = angle_matrix(bones)
    sort_indices = np.unravel_index(np.argsort(am, axis=None), am.shape)
    # Two indices that composed the largest angle
    max_ind1, max_ind2 = sort_indices[0][-1], sort_indices[1][-1]
    max_angle_value = am[max_ind1, max_ind2]

    # Find out which one is upper bone
    max_ind1, max_ind2 = make_ind1_upper(max_ind1, max_ind2, pair_lr_value)
    return max_ind1, max_ind2, max_angle_value




box_filter = rbf.BoxNetFilter()
def cobb_angles(np_pcm, np_paf, img, spine_range, use_filter=True):
    # Return np array of [a1, a2, a3]
    paf_confidence_lowerbound = 0.7
    assert len(np_pcm.shape) == 3, "expected shape: (c,h,w)"
    assert np_pcm.shape[0] == 2, "expect 2 channels at dim 0 for l and r"
    assert len(np_paf.shape) == 3, "expected shape: (c,h,w)"
    assert np_paf.shape[0] == 1, "expect 1 channel at dim 0 for paf"
    assert len(img.shape) == 2, "expected shape: (h,w)"
    assert len(spine_range.shape) == 2, "(h,w)"
    heat_hw = np_pcm.shape[1:3]
    # [lr][xy] coordinate values
    lcrc_coords = center_coords(np_pcm)
    # [p1_len][p2_len] coincidence rate of a point to another point
    coins = coincidence_rate_from_pcm_paf(lcrc_coords, heat_hw, np_paf[0])
    # [lr][p_len] pairs of points, types are index values in lcrc_coords. equal length.
    pair_lr = pairs_with_highest_confidence(coins, confidence_lowerbound=paf_confidence_lowerbound)
    # [lr][p_len][xy], coordinate values. (sorted by bone confidence, not up to bottom)
    pair_lr_value = pair_args_to_value(pair_lr, lcrc_coords)
    # Sort pairs by y
    pair_lr_value = sort_pairs_by_y(pair_lr_value)
    if use_filter:
        pair_lr_value = box_filter.filter(pair_lr_value, img)  # Left Right
        pair_lr_value = rbf.filter_by_spine_range(spine_range, pair_lr_value)
        # Leave 16 pairs. Must be the last filter
        pair_lr_value = rbf.simple_filter(pair_lr_value)  # Top pixels

    #rbf_dict = rbf.filter(pair_lr_value)
    #pair_lr_value = rbf_dict["pair_lr_value"]
    # [p_len][xy] vector coordinates. (sorted by bone confidence, not up to bottom)
    bones = bone_vectors(pair_lr_value)
    # Index1(higher), index2(lower) of max angle; a1: max angle value
    max_ind1, max_ind2, a1 = max_angle_indices(bones, pair_lr_value)

    hmids = (pair_lr_value[0] + pair_lr_value[1]) / 2
    if not isS(hmids):
    # a2 = np.rad2deg(np.arccos(cos_angle(bones[max_ind1], np.array([1, 0]))))
        # a3 = np.rad2deg(np.arccos(cos_angle(bones[max_ind2], np.array([1, 0]))))  # but the last bone is hard to detect, so use horizontal one?
        a2 = np.rad2deg(np.arccos(cos_angle(bones[max_ind1], bones[0])))  # Use first bone
        a3 = np.rad2deg(np.arccos(cos_angle(bones[max_ind2], bones[-1])))  # Note: use last bone on submit test set gains better results
    # print(a1,  a2, a3)
    # print(max_ind1, max_ind2)
    else: # isS
        a2, a3 = handle_isS_branch(pair_lr_value, max_ind1, max_ind2, np_paf.shape[1])

    result_dict = {"angles": np.array([a1, a2, a3]), "pair_lr_value": pair_lr_value}
    if img is not None:
        assert len(img.shape) == 2, "expected shape: (h,w)"
        plot_img = draw_pairs(pair_lr_value, heat_hw, img)
        result_dict["pairs_img"] = plot_img

    return result_dict

def SMAPE(pred_angles, true_angles):
    # symmetric mean absolute percentage error
    pred_angles = np.array(pred_angles)
    true_angles = np.array(true_angles)
    assert pred_angles.shape==(3,)
    assert true_angles.shape==(3,)
    minus = np.sum(np.abs(pred_angles-true_angles))
    sums = np.sum(pred_angles+true_angles)
    APE = minus/sums
    return APE*100.

def isS(mid_p):
    # Reimplementation of "isS" function in matlab file
    # Input: horizontal mid point list. (size: 68/2)
    # Input should be horizontal mid point of each left right point,
    # but we use "horizontal mid point of vertical midpoints" for convenience purpose

    def linefun(p):
        num = mid_p.shape[0]  # number of total points
        ll = np.zeros([num-2, 1], dtype=np.float32)  # 2-dim matrix (so we can use matrix multiplication later)
        for i in range(num-2):
            # formula: A - B
            # formula left part A: (p(i,2)-p(num,2))/(p(1,2)-p(num,2))
            # 1,2 in matlab correspond to 0,1 in python (x,y)
            if (p[0, 1] - p[num-1, 1])!=0:
                left_part = (p[i, 1] - p[num-1, 1]) / (p[0, 1] - p[num-1, 1])
            else:
                left_part = 0

            # formula right part B:(p(i,1)-p(num,1))/(p(1,1)-p(num,1))
            if (p[0, 0] - p[num-1,0])!=0:
                right_part = (p[i, 0] - p[num-1, 0]) / (p[0, 0] - p[num-1,0])
            else:
                right_part = 0

            # formula: result = A - B
            ll[i] = left_part - right_part
        return ll

    # isS
    mid_p = np.array(mid_p)
    ll = linefun(mid_p)
    ll_trans = np.transpose(ll, [1, 0])
    matrix_product = np.matmul(ll, ll_trans)
    flag = np.sum(matrix_product) != np.sum(np.abs(matrix_product))
    return flag

def handle_isS_branch(pair_lr_value, max_ind1, max_ind2, heat_height):
    pair_lr_value = np.array(pair_lr_value)
    assert len(pair_lr_value.shape) == 3
    hmids = (pair_lr_value[0] + pair_lr_value[1]) / 2  # Horizontal mid points, [p][xy]
    hmids_y = hmids[:, 1]  # [p]
    bones = bone_vectors(pair_lr_value)
    # Max angle in the upper part
    if (hmids_y[max_ind1] + hmids_y[max_ind2]) < heat_height:
        print("max angle in upper part")
        # From 1st to ind1, largest angle
        if max_ind1==0:
            print("max_ind1 is already the first bone")
        top_bones = bones[:max_ind1+1]  # Bones already sorted by y
        # [len_b][len_b] angle matrix
        am = angle_matrix(top_bones)
        # We want comparison of each top bones with just "ind1"
        # av: angle vector of each top bones with ind1
        av = am[-1]
        a2 = np.max(av)

        # From last to ind2, largest angle
        if max_ind2 == bones.shape[0]-1:
            print("max_ind2 is already the last bone")
        end_bones = bones[max_ind2:]
        am = angle_matrix(end_bones)
        av = am[0]
        a3 = np.max(av)
        return a2, a3
    else: # Max angle in lower part
        print("max angle in lower part")
        if max_ind1==0: print("max_ind1 is already the first bone")
        top_bones = bones[:max_ind1+1]  # Bones already sorted by y
        am = angle_matrix(top_bones)
        av = am[-1]
        a2 = np.max(av)
        arg_order = np.argsort(av)
        top_max_index = arg_order[-1] # pos1_1
        # First to pos1_1
        top_bones = bones[:top_max_index+1]
        am = angle_matrix(top_bones)
        av = am[-1]
        a3 = np.max(av)
        return a2, a3


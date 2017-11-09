from __future__ import absolute_import, division, print_function, unicode_literals
from collections import OrderedDict
from functools import partial
from importlib import import_module
import os

import numpy as np
from sklearn.base import BaseEstimator, _pprint
from xarray_filters.mldataset import MLDataset
from xarray_filters.reshape import to_features, to_xy_arrays
from xarray_filters.func_signatures import filter_args_kwargs
from xarray_filters.constants import FEATURES_LAYER_DIMS, FEATURES_LAYER
from elm.mldataset.util import _split_transformer_result
import xarray as xr
import yaml


def get_row_index(X, features_layer=None):
    '''Get the row index of a Dataset/MLDataset with a .features DataArray'''
    if features_layer is None:
        features_layer = FEATURES_LAYER
    if isinstance(X, MLDataset):
        arr = X[features_layer]
        return getattr(arr, arr.dims[0])


def _as_numpy_arrs(self, X, y=None, **kw):
    '''Convert X, y for a scikit-learn method numpy.ndarrays
    '''
    X, y = _split_transformer_result(X, y)
    if isinstance(X, (xr.Dataset, MLDataset)):
        X = MLDataset(X).to_features()
    if isinstance(y, (xr.Dataset, MLDataset)):
        y = MLDataset(y).to_features()
    row_idx = get_row_index(X)
    X, y = to_xy_arrays(X, y=y)
    if row_idx is not None:
        self._temp_row_idx = row_idx
    return X, y, row_idx


def _from_numpy_arrs(self, y, row_idx, features_layer=None):
    '''Convert a 1D prediction to ND using the row_idx MultiIndex'''
    if isinstance(y, MLDataset) or row_idx is None:
        return y
    features_layer = features_layer or FEATURES_LAYER
    coords = [row_idx,
              (FEATURES_LAYER_DIMS[1], np.array(['predict']))]
    dims = FEATURES_LAYER_DIMS
    if y.ndim == 1:
        y = y[:, np.newaxis]
    arr = xr.DataArray(y, coords=coords, dims=dims)
    dset = MLDataset(OrderedDict([(features_layer, arr)]))
    return dset.from_features(features_layer=features_layer)


class SklearnMixin:
    _cls = None
    _as_numpy_arrs = _as_numpy_arrs
    _from_numpy_arrs = _from_numpy_arrs

    def _call_sk_method(self, sk_method, X=None, y=None, do_split=True, **kw):
        '''Call a method of ._cls, typically an sklearn class,
        for a method that requires numpy arrays'''
        _cls = self._cls
        if _cls is None:
            raise ValueError('Define ._cls as a scikit-learn estimator')
        # Get the method of the class instance
        func = getattr(_cls, sk_method, None)
        if func is None:
            raise ValueError('{} is not an attribute of {}'.format(sk_method, _cls))
        X, y, row_idx = self._as_numpy_arrs(X, y=y)
        if row_idx is not None:
            self._temp_row_idx = row_idx
        kw.update(dict(self=self, X=X))
        if y is not None:
            kw['y'] = y
        kw = filter_args_kwargs(func, **kw)
        Xt = func(**kw)
        if do_split:
            Xt, y = _split_transformer_result(Xt, y)
            return Xt, y
        return Xt

    def _predict_steps(self, X, y=None, row_idx=None, sk_method=None, **kw):
        '''Call a prediction-related method, e.g. predict, score,
        but extract the row index of X, if it exists, so that
        y '''
        X2, y, temp_row_idx = self._as_numpy_arrs(X, y=y)
        if temp_row_idx is None:
            row_idx = temp_row_idx
        if row_idx is None:
            row_idx = getattr(self, '_temp_row_idx', None)
        if y is not None:
            kw['y'] = y
        out = self._call_sk_method(sk_method, X2, do_split=True, **kw)
        return out, row_idx

    def predict(self, X, row_idx=None, **kw):
        '''Predict from MLDataset X and return an MLDataset with
        DataArray called "predict" that has the dimensions of
        X's MultiIndex.  That MultiIndex typically comes from
        having called X = X.to_features() before this method. If
        X does not have a .features DataArray then X.to_features()
        is called.

        TODO - docstrings / consistency on the following methods:
            predict
            predict_proba
            predict_log_proba
            decision_function
            fit_predict
            (any other prediction-related methods I may have forgotten?)

        TODO - Note in most cases the documentation on all the methods
        should be taken from the corresponding methods of the ._cls, with
        a note about Dataset/MLDataset input/output as X, y being the
        difference.
        '''
        y, row_idx = self._predict_steps(X, row_idx=row_idx,
                                         sk_method='predict', **kw)
        y = y[0]
        if row_idx is None or getattr(self, '_predict_as_np', False):
            return y
        return self._from_numpy_arrs(y, row_idx)

    def predict_proba(self, X, row_idx=None, **kw):
        proba, row_idx = self._predict_steps(X, row_idx=row_idx,
                                             sk_method='predict_proba', **kw)
        return proba[0]

    def predict_log_proba(self, X, row_idx=None, **kw):
        log_proba, row_idx = self._predict_steps(X, row_idx=row_idx,
                                                 sk_method='predict_log_proba',
                                                 **kw)
        return log_proba[0]

    def decision_function(self, X, row_idx=None, **kw):
        d, row_idx = self._predict_steps(X, row_idx=row_idx,
                                         sk_method='decision_function',
                                         **kw)
        return d[0]

    def fit(self, X, y=None, **kw):
        self._call_sk_method('fit', X, y=y, **kw)
        return self

    def _fit(self, X, y=None, **kw):
        '''This private method is expected by some sklearn
        models and must take X, y as numpy arrays'''
        return self._call_sk_method('_fit', X, y=y, do_split=False, **kw)

    def transform(self, X, y=None, **kw):
        if hasattr(self._cls, 'transform'):
            return self._call_sk_method('transform', X, y=y, **kw)
        if hasattr(self._cls, 'fit_transform'):
            return self._call_sk_method('fit_transform', X, y=y, **kw)
        raise ValueError('Estimator {} has no "transform" or '
                         '"fit_transform" methods'.format(self))

    def fit_transform(self, X, y=None, **kw):
        args = (X,)
        if y is not None:
            args = args + (y,)
        if hasattr(self._cls, 'fit_transform'):
            return self._call_sk_method('fit_transform', *args, **kw)
        self.fit(*args, **kw)
        return self._call_sk_method('transform', *args, **kw)

    def fit_predict(self, X, y=None, **kw):
        return self.fit(X, y=y, **kw).predict(X)

    def score(self, X, y=None, sample_weight=None, row_idx=None, **kw):
        self._predict_as_np = True
        kw['sample_weight'] = sample_weight
        score, row_idx = self._predict_steps(X, row_idx=row_idx, y=y,
                                              sk_method='score',
                                              **kw)
        self._predict_as_np = False
        return score[0]


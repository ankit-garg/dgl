"""DGL PyTorch DataLoaders"""
import inspect
import math
from distutils.version import LooseVersion
import torch as th
from torch.utils.data import DataLoader
from torch.utils.data.distributed import DistributedSampler
import torch.distributed as dist
from ..dataloader import NodeCollator, EdgeCollator, GraphCollator
from ...distributed import DistGraph
from ...distributed import DistDataLoader
from ...ndarray import NDArray as DGLNDArray
from ... import backend as F
from ...base import DGLError
from ...utils import to_dgl_context

__all__ = ['NodeDataLoader', 'EdgeDataLoader', 'GraphDataLoader',
           # Temporary exposure.
           '_pop_subgraph_storage', '_pop_blocks_storage',
           '_restore_subgraph_storage', '_restore_blocks_storage']

PYTORCH_VER = LooseVersion(th.__version__)
PYTORCH_16 = PYTORCH_VER >= LooseVersion("1.6.0")
PYTORCH_17 = PYTORCH_VER >= LooseVersion("1.7.0")

def _create_dist_sampler(dataset, dataloader_kwargs, ddp_seed):
    # Note: will change the content of dataloader_kwargs
    dist_sampler_kwargs = {'shuffle': dataloader_kwargs['shuffle']}
    dataloader_kwargs['shuffle'] = False
    if PYTORCH_16:
        dist_sampler_kwargs['seed'] = ddp_seed
    if PYTORCH_17:
        dist_sampler_kwargs['drop_last'] = dataloader_kwargs['drop_last']
        dataloader_kwargs['drop_last'] = False

    return DistributedSampler(dataset, **dist_sampler_kwargs)

class _ScalarDataBatcherIter:
    def __init__(self, dataset, batch_size, drop_last):
        self.dataset = dataset
        self.batch_size = batch_size
        self.index = 0
        self.drop_last = drop_last

    # Make this an iterator for PyTorch Lightning compatibility
    def __iter__(self):
        return self

    def __next__(self):
        num_items = self.dataset.shape[0]
        if self.index >= num_items:
            raise StopIteration
        end_idx = self.index + self.batch_size
        if end_idx > num_items:
            if self.drop_last:
                raise StopIteration
            end_idx = num_items
        batch = self.dataset[self.index:end_idx]
        self.index += self.batch_size

        return batch

class _ScalarDataBatcher(th.utils.data.IterableDataset):
    """Custom Dataset wrapper to return mini-batches as tensors, rather than as
    lists. When the dataset is on the GPU, this significantly reduces
    the overhead. For the case of a batch size of 1024, instead of giving a
    list of 1024 tensors to the collator, a single tensor of 1024 dimensions
    is passed in.
    """
    def __init__(self, dataset, shuffle=False, batch_size=1,
                 drop_last=False, use_ddp=False, ddp_seed=0):
        super(_ScalarDataBatcher).__init__()
        self.dataset = dataset
        self.batch_size = batch_size
        self.shuffle = shuffle
        self.drop_last = drop_last
        self.use_ddp = use_ddp
        if use_ddp:
            self.rank = dist.get_rank()
            self.num_replicas = dist.get_world_size()
            self.seed = ddp_seed
            self.epoch = 0
            # The following code (and the idea of cross-process shuffling with the same seed)
            # comes from PyTorch.  See torch/utils/data/distributed.py for details.

            # If the dataset length is evenly divisible by # of replicas, then there
            # is no need to drop any sample, since the dataset will be split evenly.
            if self.drop_last and len(self.dataset) % self.num_replicas != 0:  # type: ignore
                # Split to nearest available length that is evenly divisible.
                # This is to ensure each rank receives the same amount of data when
                # using this Sampler.
                self.num_samples = math.ceil(
                    # `type:ignore` is required because Dataset cannot provide a default __len__
                    # see NOTE in pytorch/torch/utils/data/sampler.py
                    (len(self.dataset) - self.num_replicas) / self.num_replicas  # type: ignore
                )
            else:
                self.num_samples = math.ceil(len(self.dataset) / self.num_replicas)  # type: ignore
            self.total_size = self.num_samples * self.num_replicas

    def __iter__(self):
        if self.use_ddp:
            return self._iter_ddp()
        else:
            return self._iter_non_ddp()

    def _divide_by_worker(self, dataset):
        worker_info = th.utils.data.get_worker_info()
        if worker_info:
            # worker gets only a fraction of the dataset
            chunk_size = dataset.shape[0] // worker_info.num_workers
            left_over = dataset.shape[0] % worker_info.num_workers
            start = (chunk_size*worker_info.id) + min(left_over, worker_info.id)
            end = start + chunk_size + (worker_info.id < left_over)
            assert worker_info.id < worker_info.num_workers-1 or \
                end == dataset.shape[0]
            dataset = dataset[start:end]

        return dataset

    def _iter_non_ddp(self):
        dataset = self._divide_by_worker(self.dataset)

        if self.shuffle:
            # permute the dataset
            perm = th.randperm(dataset.shape[0], device=dataset.device)
            dataset = dataset[perm]

        return _ScalarDataBatcherIter(dataset, self.batch_size, self.drop_last)

    def _iter_ddp(self):
        # The following code (and the idea of cross-process shuffling with the same seed)
        # comes from PyTorch.  See torch/utils/data/distributed.py for details.
        if self.shuffle:
            # deterministically shuffle based on epoch and seed
            g = th.Generator()
            g.manual_seed(self.seed + self.epoch)
            indices = th.randperm(len(self.dataset), generator=g)
        else:
            indices = th.arange(len(self.dataset))

        if not self.drop_last:
            # add extra samples to make it evenly divisible
            indices = th.cat([indices, indices[:(self.total_size - indices.shape[0])]])
        else:
            # remove tail of data to make it evenly divisible.
            indices = indices[:self.total_size]
        assert indices.shape[0] == self.total_size

        # subsample
        indices = indices[self.rank:self.total_size:self.num_replicas]
        assert indices.shape[0] == self.num_samples

        # Dividing by worker is our own stuff.
        dataset = self._divide_by_worker(self.dataset[indices])
        return _ScalarDataBatcherIter(dataset, self.batch_size, self.drop_last)

    def __len__(self):
        num_samples = self.num_samples if self.use_ddp else self.dataset.shape[0]
        return (num_samples + (0 if self.drop_last else self.batch_size - 1)) // self.batch_size

    def set_epoch(self, epoch):
        """Set epoch number for distributed training."""
        self.epoch = epoch

def _remove_kwargs_dist(kwargs):
    if 'num_workers' in kwargs:
        del kwargs['num_workers']
    if 'pin_memory' in kwargs:
        del kwargs['pin_memory']
        print('Distributed DataLoader does not support pin_memory')
    return kwargs

# The following code is a fix to the PyTorch-specific issue in
# https://github.com/dmlc/dgl/issues/2137
#
# Basically the sampled MFGs/subgraphs contain the features extracted from the
# parent graph.  In DGL, the MFGs/subgraphs will hold a reference to the parent
# graph feature tensor and an index tensor, so that the features could be extracted upon
# request.  However, in the context of multiprocessed sampling, we do not need to
# transmit the parent graph feature tensor from the subprocess to the main process,
# since they are exactly the same tensor, and transmitting a tensor from a subprocess
# to the main process is costly in PyTorch as it uses shared memory.  We work around
# it with the following trick:
#
# In the collator running in the sampler processes:
# For each frame in the MFG, we check each column and the column with the same name
# in the corresponding parent frame.  If the storage of the former column is the
# same object as the latter column, we are sure that the former column is a
# subcolumn of the latter, and set the storage of the former column as None.
#
# In the iterator of the main process:
# For each frame in the MFG, we check each column and the column with the same name
# in the corresponding parent frame.  If the storage of the former column is None,
# we replace it with the storage of the latter column.

def _pop_subframe_storage(subframe, frame):
    for key, col in subframe._columns.items():
        if key in frame._columns and col.storage is frame._columns[key].storage:
            col.storage = None

def _pop_subgraph_storage(subg, g):
    for ntype in subg.ntypes:
        if ntype not in g.ntypes:
            continue
        subframe = subg._node_frames[subg.get_ntype_id(ntype)]
        frame = g._node_frames[g.get_ntype_id(ntype)]
        _pop_subframe_storage(subframe, frame)
    for etype in subg.canonical_etypes:
        if etype not in g.canonical_etypes:
            continue
        subframe = subg._edge_frames[subg.get_etype_id(etype)]
        frame = g._edge_frames[g.get_etype_id(etype)]
        _pop_subframe_storage(subframe, frame)

def _pop_blocks_storage(blocks, g):
    for block in blocks:
        for ntype in block.srctypes:
            if ntype not in g.ntypes:
                continue
            subframe = block._node_frames[block.get_ntype_id_from_src(ntype)]
            frame = g._node_frames[g.get_ntype_id(ntype)]
            _pop_subframe_storage(subframe, frame)
        for ntype in block.dsttypes:
            if ntype not in g.ntypes:
                continue
            subframe = block._node_frames[block.get_ntype_id_from_dst(ntype)]
            frame = g._node_frames[g.get_ntype_id(ntype)]
            _pop_subframe_storage(subframe, frame)
        for etype in block.canonical_etypes:
            if etype not in g.canonical_etypes:
                continue
            subframe = block._edge_frames[block.get_etype_id(etype)]
            frame = g._edge_frames[g.get_etype_id(etype)]
            _pop_subframe_storage(subframe, frame)

def _restore_subframe_storage(subframe, frame):
    for key, col in subframe._columns.items():
        if col.storage is None:
            col.storage = frame._columns[key].storage

def _restore_subgraph_storage(subg, g):
    for ntype in subg.ntypes:
        if ntype not in g.ntypes:
            continue
        subframe = subg._node_frames[subg.get_ntype_id(ntype)]
        frame = g._node_frames[g.get_ntype_id(ntype)]
        _restore_subframe_storage(subframe, frame)
    for etype in subg.canonical_etypes:
        if etype not in g.canonical_etypes:
            continue
        subframe = subg._edge_frames[subg.get_etype_id(etype)]
        frame = g._edge_frames[g.get_etype_id(etype)]
        _restore_subframe_storage(subframe, frame)

def _restore_blocks_storage(blocks, g):
    for block in blocks:
        for ntype in block.srctypes:
            if ntype not in g.ntypes:
                continue
            subframe = block._node_frames[block.get_ntype_id_from_src(ntype)]
            frame = g._node_frames[g.get_ntype_id(ntype)]
            _restore_subframe_storage(subframe, frame)
        for ntype in block.dsttypes:
            if ntype not in g.ntypes:
                continue
            subframe = block._node_frames[block.get_ntype_id_from_dst(ntype)]
            frame = g._node_frames[g.get_ntype_id(ntype)]
            _restore_subframe_storage(subframe, frame)
        for etype in block.canonical_etypes:
            if etype not in g.canonical_etypes:
                continue
            subframe = block._edge_frames[block.get_etype_id(etype)]
            frame = g._edge_frames[g.get_etype_id(etype)]
            _restore_subframe_storage(subframe, frame)

class _NodeCollator(NodeCollator):
    def collate(self, items):
        # input_nodes, output_nodes, blocks
        result = super().collate(items)
        _pop_blocks_storage(result[-1], self.g)
        return result

class _EdgeCollator(EdgeCollator):
    def collate(self, items):
        if self.negative_sampler is None:
            # input_nodes, pair_graph, blocks
            result = super().collate(items)
            _pop_subgraph_storage(result[1], self.g)
            _pop_blocks_storage(result[-1], self.g_sampling)
            return result
        else:
            # input_nodes, pair_graph, neg_pair_graph, blocks
            result = super().collate(items)
            _pop_subgraph_storage(result[1], self.g)
            _pop_subgraph_storage(result[2], self.g)
            _pop_blocks_storage(result[-1], self.g_sampling)
            return result

def _to_device(data, device):
    if isinstance(data, dict):
        for k, v in data.items():
            data[k] = v.to(device)
    elif isinstance(data, list):
        data = [item.to(device) for item in data]
    else:
        data = data.to(device)
    return data

class _NodeDataLoaderIter:
    def __init__(self, node_dataloader):
        self.device = node_dataloader.device
        self.node_dataloader = node_dataloader
        self.iter_ = iter(node_dataloader.dataloader)

    # Make this an iterator for PyTorch Lightning compatibility
    def __iter__(self):
        return self

    def __next__(self):
        # input_nodes, output_nodes, blocks
        result_ = next(self.iter_)
        _restore_blocks_storage(result_[-1], self.node_dataloader.collator.g)

        result = [_to_device(data, self.device) for data in result_]
        return result

class _EdgeDataLoaderIter:
    def __init__(self, edge_dataloader):
        self.device = edge_dataloader.device
        self.edge_dataloader = edge_dataloader
        self.iter_ = iter(edge_dataloader.dataloader)

    # Make this an iterator for PyTorch Lightning compatibility
    def __iter__(self):
        return self

    def __next__(self):
        result_ = next(self.iter_)

        if self.edge_dataloader.collator.negative_sampler is not None:
            # input_nodes, pair_graph, neg_pair_graph, blocks if None.
            # Otherwise, input_nodes, pair_graph, blocks
            _restore_subgraph_storage(result_[2], self.edge_dataloader.collator.g)
        _restore_subgraph_storage(result_[1], self.edge_dataloader.collator.g)
        _restore_blocks_storage(result_[-1], self.edge_dataloader.collator.g_sampling)

        result = [_to_device(data, self.device) for data in result_]
        return result

def _init_dataloader(collator, device, dataloader_kwargs, use_ddp, ddp_seed):
    dataset = collator.dataset
    use_scalar_batcher = False
    scalar_batcher = None

    if th.device(device) != th.device('cpu') and dataloader_kwargs.get('num_workers', 0) != 0:
        batch_size = dataloader_kwargs.get('batch_size', 1)

        if batch_size > 1:
            if isinstance(dataset, DGLNDArray):
                # the dataset needs to be a torch tensor for the
                # _ScalarDataBatcher
                dataset = F.zerocopy_from_dgl_ndarray(dataset)
            if isinstance(dataset, th.Tensor):
                shuffle = dataloader_kwargs.get('shuffle', False)
                drop_last = dataloader_kwargs.get('drop_last', False)
                # manually batch into tensors
                dataset = _ScalarDataBatcher(dataset,
                                             batch_size=batch_size,
                                             shuffle=shuffle,
                                             drop_last=drop_last,
                                             use_ddp=use_ddp,
                                             ddp_seed=ddp_seed)
                # need to overwrite things that will be handled by the batcher
                dataloader_kwargs['batch_size'] = None
                dataloader_kwargs['shuffle'] = False
                dataloader_kwargs['drop_last'] = False
                use_scalar_batcher = True
                scalar_batcher = dataset

    if use_ddp and not use_scalar_batcher:
        dist_sampler = _create_dist_sampler(dataset, dataloader_kwargs, ddp_seed)
        dataloader_kwargs['sampler'] = dist_sampler
    else:
        dist_sampler = None

    dataloader = DataLoader(
        dataset,
        collate_fn=collator.collate,
        **dataloader_kwargs)

    return use_scalar_batcher, scalar_batcher, dataloader, dist_sampler

class NodeDataLoader:
    """PyTorch dataloader for batch-iterating over a set of nodes, generating the list
    of message flow graphs (MFGs) as computation dependency of the said minibatch.

    Parameters
    ----------
    g : DGLGraph
        The graph.
    nids : Tensor or dict[ntype, Tensor]
        The node set to compute outputs.
    block_sampler : dgl.dataloading.BlockSampler
        The neighborhood sampler.
    device : device context, optional
        The device of the generated MFGs in each iteration, which should be a
        PyTorch device object (e.g., ``torch.device``).

        By default this value is the same as the device of :attr:`g`.
    use_ddp : boolean, optional
        If True, tells the DataLoader to split the training set for each
        participating process appropriately using
        :class:`torch.utils.data.distributed.DistributedSampler`.

        Note that :func:`~dgl.dataloading.NodeDataLoader.set_epoch` must be called
        at the beginning of every epoch if :attr:`use_ddp` is True.

        Overrides the :attr:`sampler` argument of :class:`torch.utils.data.DataLoader`.
    ddp_seed : int, optional
        The seed for shuffling the dataset in
        :class:`torch.utils.data.distributed.DistributedSampler`.

        Only effective when :attr:`use_ddp` is True.
    kwargs : dict
        Arguments being passed to :py:class:`torch.utils.data.DataLoader`.

    Examples
    --------
    To train a 3-layer GNN for node classification on a set of nodes ``train_nid`` on
    a homogeneous graph where each node takes messages from all neighbors (assume
    the backend is PyTorch):

    >>> sampler = dgl.dataloading.MultiLayerNeighborSampler([15, 10, 5])
    >>> dataloader = dgl.dataloading.NodeDataLoader(
    ...     g, train_nid, sampler,
    ...     batch_size=1024, shuffle=True, drop_last=False, num_workers=4)
    >>> for input_nodes, output_nodes, blocks in dataloader:
    ...     train_on(input_nodes, output_nodes, blocks)

    **Using with Distributed Data Parallel**

    If you are using PyTorch's distributed training (e.g. when using
    :mod:`torch.nn.parallel.DistributedDataParallel`), you can train the model by turning
    on the `use_ddp` option:

    >>> sampler = dgl.dataloading.MultiLayerNeighborSampler([15, 10, 5])
    >>> dataloader = dgl.dataloading.NodeDataLoader(
    ...     g, train_nid, sampler, use_ddp=True,
    ...     batch_size=1024, shuffle=True, drop_last=False, num_workers=4)
    >>> for epoch in range(start_epoch, n_epochs):
    ...     dataloader.set_epoch(epoch)
    ...     for input_nodes, output_nodes, blocks in dataloader:
    ...         train_on(input_nodes, output_nodes, blocks)

    Notes
    -----
    Please refer to
    :doc:`Minibatch Training Tutorials <tutorials/large/L0_neighbor_sampling_overview>`
    and :ref:`User Guide Section 6 <guide-minibatch>` for usage.

    **Tips for selecting the proper device**

    * If the input graph :attr:`g` is on GPU, the output device :attr:`device` must be the same GPU
      and :attr:`num_workers` must be zero. In this case, the sampling and subgraph construction
      will take place on the GPU. This is the recommended setting when using a single-GPU and
      the whole graph fits in GPU memory.

    * If the input graph :attr:`g` is on CPU while the output device :attr:`device` is GPU, then
      depending on the value of :attr:`num_workers`:

      - If :attr:`num_workers` is set to 0, the sampling will happen on the CPU, and then the
        subgraphs will be constructed directly on the GPU. This is the recommend setting in
        multi-GPU configurations.

      - Otherwise, if :attr:`num_workers` is greater than 0, both the sampling and subgraph
        construction will take place on the CPU. This is the recommended setting when using a
        single-GPU and the whole graph does not fit in GPU memory.
    """
    collator_arglist = inspect.getfullargspec(NodeCollator).args

    def __init__(self, g, nids, block_sampler, device=None, use_ddp=False, ddp_seed=0, **kwargs):
        collator_kwargs = {}
        dataloader_kwargs = {}
        for k, v in kwargs.items():
            if k in self.collator_arglist:
                collator_kwargs[k] = v
            else:
                dataloader_kwargs[k] = v

        if isinstance(g, DistGraph):
            if device is None:
                # for the distributed case default to the CPU
                device = 'cpu'
            assert device == 'cpu', 'Only cpu is supported in the case of a DistGraph.'
            # Distributed DataLoader currently does not support heterogeneous graphs
            # and does not copy features.  Fallback to normal solution
            self.collator = NodeCollator(g, nids, block_sampler, **collator_kwargs)
            _remove_kwargs_dist(dataloader_kwargs)
            self.dataloader = DistDataLoader(self.collator.dataset,
                                             collate_fn=self.collator.collate,
                                             **dataloader_kwargs)
            self.is_distributed = True
        else:
            if device is None:
                # default to the same device the graph is on
                device = th.device(g.device)

            # if the sampler supports it, tell it to output to the
            # specified device
            num_workers = dataloader_kwargs.get('num_workers', 0)
            if callable(getattr(block_sampler, "set_output_context", None)) and num_workers == 0:
                block_sampler.set_output_context(to_dgl_context(device))

            self.collator = _NodeCollator(g, nids, block_sampler, **collator_kwargs)
            self.use_scalar_batcher, self.scalar_batcher, self.dataloader, self.dist_sampler = \
                _init_dataloader(self.collator, device, dataloader_kwargs, use_ddp, ddp_seed)

            self.use_ddp = use_ddp
            self.is_distributed = False

            # Precompute the CSR and CSC representations so each subprocess does not
            # duplicate.
            if num_workers > 0:
                g.create_formats_()
        self.device = device

    def __iter__(self):
        """Return the iterator of the data loader."""
        if self.is_distributed:
            # Directly use the iterator of DistDataLoader, which doesn't copy features anyway.
            return iter(self.dataloader)
        else:
            return _NodeDataLoaderIter(self)

    def __len__(self):
        """Return the number of batches of the data loader."""
        return len(self.dataloader)

    def set_epoch(self, epoch):
        """Sets the epoch number for the underlying sampler which ensures all replicas
        to use a different ordering for each epoch.

        Only available when :attr:`use_ddp` is True.

        Calls :meth:`torch.utils.data.distributed.DistributedSampler.set_epoch`.

        Parameters
        ----------
        epoch : int
            The epoch number.
        """
        if self.use_ddp:
            if self.use_scalar_batcher:
                self.scalar_batcher.set_epoch(epoch)
            else:
                self.dist_sampler.set_epoch(epoch)
        else:
            raise DGLError('set_epoch is only available when use_ddp is True.')

class EdgeDataLoader:
    """PyTorch dataloader for batch-iterating over a set of edges, generating the list
    of message flow graphs (MFGs) as computation dependency of the said minibatch for
    edge classification, edge regression, and link prediction.

    For each iteration, the object will yield

    * A tensor of input nodes necessary for computing the representation on edges, or
      a dictionary of node type names and such tensors.

    * A subgraph that contains only the edges in the minibatch and their incident nodes.
      Note that the graph has an identical metagraph with the original graph.

    * If a negative sampler is given, another graph that contains the "negative edges",
      connecting the source and destination nodes yielded from the given negative sampler.

    * A list of MFGs necessary for computing the representation of the incident nodes
      of the edges in the minibatch.

    For more details, please refer to :ref:`guide-minibatch-edge-classification-sampler`
    and :ref:`guide-minibatch-link-classification-sampler`.

    Parameters
    ----------
    g : DGLGraph
        The graph.  Currently must be on CPU; GPU is not supported.
    eids : Tensor or dict[etype, Tensor]
        The edge set in graph :attr:`g` to compute outputs.
    block_sampler : dgl.dataloading.BlockSampler
        The neighborhood sampler.
    device : device context, optional
        The device of the generated MFGs and graphs in each iteration, which should be a
        PyTorch device object (e.g., ``torch.device``).

        By default this value is the same as the device of :attr:`g`.
    g_sampling : DGLGraph, optional
        The graph where neighborhood sampling is performed.

        One may wish to iterate over the edges in one graph while perform sampling in
        another graph.  This may be the case for iterating over validation and test
        edge set while perform neighborhood sampling on the graph formed by only
        the training edge set.

        If None, assume to be the same as ``g``.
    exclude : str, optional
        Whether and how to exclude dependencies related to the sampled edges in the
        minibatch.  Possible values are

        * None,
        * ``self``,
        * ``reverse_id``,
        * ``reverse_types``

        See the description of the argument with the same name in the docstring of
        :class:`~dgl.dataloading.EdgeCollator` for more details.
    reverse_eids : Tensor or dict[etype, Tensor], optional
        A tensor of reverse edge ID mapping.  The i-th element indicates the ID of
        the i-th edge's reverse edge.

        If the graph is heterogeneous, this argument requires a dictionary of edge
        types and the reverse edge ID mapping tensors.

        See the description of the argument with the same name in the docstring of
        :class:`~dgl.dataloading.EdgeCollator` for more details.
    reverse_etypes : dict[etype, etype], optional
        The mapping from the original edge types to their reverse edge types.

        See the description of the argument with the same name in the docstring of
        :class:`~dgl.dataloading.EdgeCollator` for more details.
    negative_sampler : callable, optional
        The negative sampler.

        See the description of the argument with the same name in the docstring of
        :class:`~dgl.dataloading.EdgeCollator` for more details.
    use_ddp : boolean, optional
        If True, tells the DataLoader to split the training set for each
        participating process appropriately using
        :mod:`torch.utils.data.distributed.DistributedSampler`.

        Note that :func:`~dgl.dataloading.NodeDataLoader.set_epoch` must be called
        at the beginning of every epoch if :attr:`use_ddp` is True.

        The dataloader will have a :attr:`dist_sampler` attribute to set the
        epoch number, as recommended by PyTorch.

        Overrides the :attr:`sampler` argument of :class:`torch.utils.data.DataLoader`.
    ddp_seed : int, optional
        The seed for shuffling the dataset in
        :class:`torch.utils.data.distributed.DistributedSampler`.

        Only effective when :attr:`use_ddp` is True.
    kwargs : dict
        Arguments being passed to :py:class:`torch.utils.data.DataLoader`.

    Examples
    --------
    The following example shows how to train a 3-layer GNN for edge classification on a
    set of edges ``train_eid`` on a homogeneous undirected graph.  Each node takes
    messages from all neighbors.

    Say that you have an array of source node IDs ``src`` and another array of destination
    node IDs ``dst``.  One can make it bidirectional by adding another set of edges
    that connects from ``dst`` to ``src``:

    >>> g = dgl.graph((torch.cat([src, dst]), torch.cat([dst, src])))

    One can then know that the ID difference of an edge and its reverse edge is ``|E|``,
    where ``|E|`` is the length of your source/destination array.  The reverse edge
    mapping can be obtained by

    >>> E = len(src)
    >>> reverse_eids = torch.cat([torch.arange(E, 2 * E), torch.arange(0, E)])

    Note that the sampled edges as well as their reverse edges are removed from
    computation dependencies of the incident nodes.  That is, the edge will not
    involve in neighbor sampling and message aggregation.  This is a common trick
    to avoid information leakage.

    >>> sampler = dgl.dataloading.MultiLayerNeighborSampler([15, 10, 5])
    >>> dataloader = dgl.dataloading.EdgeDataLoader(
    ...     g, train_eid, sampler, exclude='reverse_id',
    ...     reverse_eids=reverse_eids,
    ...     batch_size=1024, shuffle=True, drop_last=False, num_workers=4)
    >>> for input_nodes, pair_graph, blocks in dataloader:
    ...     train_on(input_nodes, pair_graph, blocks)

    To train a 3-layer GNN for link prediction on a set of edges ``train_eid`` on a
    homogeneous graph where each node takes messages from all neighbors (assume the
    backend is PyTorch), with 5 uniformly chosen negative samples per edge:

    >>> sampler = dgl.dataloading.MultiLayerNeighborSampler([15, 10, 5])
    >>> neg_sampler = dgl.dataloading.negative_sampler.Uniform(5)
    >>> dataloader = dgl.dataloading.EdgeDataLoader(
    ...     g, train_eid, sampler, exclude='reverse_id',
    ...     reverse_eids=reverse_eids, negative_sampler=neg_sampler,
    ...     batch_size=1024, shuffle=True, drop_last=False, num_workers=4)
    >>> for input_nodes, pos_pair_graph, neg_pair_graph, blocks in dataloader:
    ...     train_on(input_nodse, pair_graph, neg_pair_graph, blocks)

    For heterogeneous graphs, the reverse of an edge may have a different edge type
    from the original edge.  For instance, consider that you have an array of
    user-item clicks, representated by a user array ``user`` and an item array ``item``.
    You may want to build a heterogeneous graph with a user-click-item relation and an
    item-clicked-by-user relation.

    >>> g = dgl.heterograph({
    ...     ('user', 'click', 'item'): (user, item),
    ...     ('item', 'clicked-by', 'user'): (item, user)})

    To train a 3-layer GNN for edge classification on a set of edges ``train_eid`` with
    type ``click``, you can write

    >>> sampler = dgl.dataloading.MultiLayerNeighborSampler([15, 10, 5])
    >>> dataloader = dgl.dataloading.EdgeDataLoader(
    ...     g, {'click': train_eid}, sampler, exclude='reverse_types',
    ...     reverse_etypes={'click': 'clicked-by', 'clicked-by': 'click'},
    ...     batch_size=1024, shuffle=True, drop_last=False, num_workers=4)
    >>> for input_nodes, pair_graph, blocks in dataloader:
    ...     train_on(input_nodes, pair_graph, blocks)

    To train a 3-layer GNN for link prediction on a set of edges ``train_eid`` with type
    ``click``, you can write

    >>> sampler = dgl.dataloading.MultiLayerNeighborSampler([15, 10, 5])
    >>> neg_sampler = dgl.dataloading.negative_sampler.Uniform(5)
    >>> dataloader = dgl.dataloading.EdgeDataLoader(
    ...     g, train_eid, sampler, exclude='reverse_types',
    ...     reverse_etypes={'click': 'clicked-by', 'clicked-by': 'click'},
    ...     negative_sampler=neg_sampler,
    ...     batch_size=1024, shuffle=True, drop_last=False, num_workers=4)
    >>> for input_nodes, pos_pair_graph, neg_pair_graph, blocks in dataloader:
    ...     train_on(input_nodes, pair_graph, neg_pair_graph, blocks)

    **Using with Distributed Data Parallel**

    If you are using PyTorch's distributed training (e.g. when using
    :mod:`torch.nn.parallel.DistributedDataParallel`), you can train the model by
    turning on the :attr:`use_ddp` option:

    >>> sampler = dgl.dataloading.MultiLayerNeighborSampler([15, 10, 5])
    >>> dataloader = dgl.dataloading.EdgeDataLoader(
    ...     g, train_eid, sampler, use_ddp=True, exclude='reverse_id',
    ...     reverse_eids=reverse_eids,
    ...     batch_size=1024, shuffle=True, drop_last=False, num_workers=4)
    >>> for epoch in range(start_epoch, n_epochs):
    ...     dataloader.set_epoch(epoch)
    ...     for input_nodes, pair_graph, blocks in dataloader:
    ...         train_on(input_nodes, pair_graph, blocks)

    See also
    --------
    dgl.dataloading.dataloader.EdgeCollator

    Notes
    -----
    Please refer to
    :doc:`Minibatch Training Tutorials <tutorials/large/L0_neighbor_sampling_overview>`
    and :ref:`User Guide Section 6 <guide-minibatch>` for usage.

    For end-to-end usages, please refer to the following tutorial/examples:

    * Edge classification on heterogeneous graph: GCMC

    * Link prediction on homogeneous graph: GraphSAGE for unsupervised learning

    * Link prediction on heterogeneous graph: RGCN for link prediction.
    """
    collator_arglist = inspect.getfullargspec(EdgeCollator).args

    def __init__(self, g, eids, block_sampler, device='cpu', use_ddp=False, ddp_seed=0, **kwargs):
        collator_kwargs = {}
        dataloader_kwargs = {}
        for k, v in kwargs.items():
            if k in self.collator_arglist:
                collator_kwargs[k] = v
            else:
                dataloader_kwargs[k] = v

        if isinstance(g, DistGraph):
            if device is None:
                # for the distributed case default to the CPU
                device = 'cpu'
            assert device == 'cpu', 'Only cpu is supported in the case of a DistGraph.'
            # Distributed DataLoader currently does not support heterogeneous graphs
            # and does not copy features.  Fallback to normal solution
            self.collator = EdgeCollator(g, eids, block_sampler, **collator_kwargs)
            _remove_kwargs_dist(dataloader_kwargs)
            self.dataloader = DistDataLoader(self.collator.dataset,
                                             collate_fn=self.collator.collate,
                                             **dataloader_kwargs)
            self.is_distributed = True
        else:
            if device is None:
                # default to the same device the graph is on
                device = th.device(g.device)

            # if the sampler supports it, tell it to output to the
            # specified device
            num_workers = dataloader_kwargs.get('num_workers', 0)
            if callable(getattr(block_sampler, "set_output_context", None)) and num_workers == 0:
                block_sampler.set_output_context(to_dgl_context(device))

            self.collator = _EdgeCollator(g, eids, block_sampler, **collator_kwargs)
            self.use_scalar_batcher, self.scalar_batcher, self.dataloader, self.dist_sampler = \
                    _init_dataloader(self.collator, device, dataloader_kwargs, use_ddp, ddp_seed)
            self.use_ddp = use_ddp
            self.is_distributed = False

            # Precompute the CSR and CSC representations so each subprocess does not duplicate.
            if num_workers > 0:
                g.create_formats_()

        self.device = device

    def __iter__(self):
        """Return the iterator of the data loader."""
        if self.is_distributed:
            # Directly use the iterator of DistDataLoader, which doesn't copy features anyway.
            return iter(self.dataloader)
        else:
            return _EdgeDataLoaderIter(self)

    def __len__(self):
        """Return the number of batches of the data loader."""
        return len(self.dataloader)

    def set_epoch(self, epoch):
        """Sets the epoch number for the underlying sampler which ensures all replicas
        to use a different ordering for each epoch.

        Only available when :attr:`use_ddp` is True.

        Calls :meth:`torch.utils.data.distributed.DistributedSampler.set_epoch`.

        Parameters
        ----------
        epoch : int
            The epoch number.
        """
        if self.use_ddp:
            if self.use_scalar_batcher:
                self.scalar_batcher.set_epoch(epoch)
            else:
                self.dist_sampler.set_epoch(epoch)
        else:
            raise DGLError('set_epoch is only available when use_ddp is True.')

class GraphDataLoader:
    """PyTorch dataloader for batch-iterating over a set of graphs, generating the batched
    graph and corresponding label tensor (if provided) of the said minibatch.

    Parameters
    ----------
    collate_fn : Function, default is None
        The customized collate function. Will use the default collate
        function if not given.
    use_ddp : boolean, optional
        If True, tells the DataLoader to split the training set for each
        participating process appropriately using
        :class:`torch.utils.data.distributed.DistributedSampler`.

        Overrides the :attr:`sampler` argument of :class:`torch.utils.data.DataLoader`.
    ddp_seed : int, optional
        The seed for shuffling the dataset in
        :class:`torch.utils.data.distributed.DistributedSampler`.

        Only effective when :attr:`use_ddp` is True.
    kwargs : dict
        Arguments being passed to :py:class:`torch.utils.data.DataLoader`.

    Examples
    --------
    To train a GNN for graph classification on a set of graphs in ``dataset`` (assume
    the backend is PyTorch):

    >>> dataloader = dgl.dataloading.GraphDataLoader(
    ...     dataset, batch_size=1024, shuffle=True, drop_last=False, num_workers=4)
    >>> for batched_graph, labels in dataloader:
    ...     train_on(batched_graph, labels)

    **Using with Distributed Data Parallel**

    If you are using PyTorch's distributed training (e.g. when using
    :mod:`torch.nn.parallel.DistributedDataParallel`), you can train the model by
    turning on the :attr:`use_ddp` option:

    >>> dataloader = dgl.dataloading.GraphDataLoader(
    ...     dataset, use_ddp=True, batch_size=1024, shuffle=True, drop_last=False, num_workers=4)
    >>> for epoch in range(start_epoch, n_epochs):
    ...     dataloader.set_epoch(epoch)
    ...     for batched_graph, labels in dataloader:
    ...         train_on(batched_graph, labels)
    """
    collator_arglist = inspect.getfullargspec(GraphCollator).args

    def __init__(self, dataset, collate_fn=None, use_ddp=False, ddp_seed=0, **kwargs):
        collator_kwargs = {}
        dataloader_kwargs = {}
        for k, v in kwargs.items():
            if k in self.collator_arglist:
                collator_kwargs[k] = v
            else:
                dataloader_kwargs[k] = v

        if collate_fn is None:
            self.collate = GraphCollator(**collator_kwargs).collate
        else:
            self.collate = collate_fn

        self.use_ddp = use_ddp
        if use_ddp:
            self.dist_sampler = _create_dist_sampler(dataset, dataloader_kwargs, ddp_seed)
            dataloader_kwargs['sampler'] = self.dist_sampler

        self.dataloader = DataLoader(dataset=dataset,
                                     collate_fn=self.collate,
                                     **dataloader_kwargs)

    def __iter__(self):
        """Return the iterator of the data loader."""
        return iter(self.dataloader)

    def __len__(self):
        """Return the number of batches of the data loader."""
        return len(self.dataloader)

    def set_epoch(self, epoch):
        """Sets the epoch number for the underlying sampler which ensures all replicas
        to use a different ordering for each epoch.

        Only available when :attr:`use_ddp` is True.

        Calls :meth:`torch.utils.data.distributed.DistributedSampler.set_epoch`.

        Parameters
        ----------
        epoch : int
            The epoch number.
        """
        if self.use_ddp:
            self.dist_sampler.set_epoch(epoch)
        else:
            raise DGLError('set_epoch is only available when use_ddp is True.')

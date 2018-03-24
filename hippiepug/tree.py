"""
Tools for building and interpreting key-value Merkle trees.
"""

import abc
import collections
import os

from .struct import TreeNode, TreeLeaf
from .pack import encode, decode


class Tree(object):
    """
    View of a Merkle tree.

    Use :py:class:`TreeBuilder` to build a Merkle tree first.

    :param object_store: Object store
    :param root: The hash of the root node
    :param cache: Cache
    :type cache: dict

    .. warning::
       All read accesses are cached. The cache is assumed to be trusted,
       so blocks retrieved from cache are not checked for integrity, unlike
       when they are retrieved from the object store.

    .. seealso::
       * :py:class:`hippiepug.chain.Chain`
    """

    def __init__(self, object_store, root,
                 cache=None):
        self.object_store = object_store
        self.root = root
        self._cache = cache or {}

    def get_node_by_hash(self, node_hash):
        """Retrieve node or object by its hash."""
        if node_hash in self._cache:
            return self._cache[node_hash]

        serialized_node = self.object_store.get(
                node_hash, check_integrity=True)
        if serialized_node is not None:
            node = decode(serialized_node)
            self._cache[node_hash] = node
            return node

    def get_inclusion_proof(self, lookup_key):
        """Get (non-)inclusion proof for a lookup key.

        :param lookup_key: Lookup key
        :returns: A tuple with a path to a leaf node, and a list of other
                  nodes needed to reproduce the tree root.
        """
        path_nodes = []
        closure_nodes = []
        current_node = self.root_node

        while not isinstance(current_node, TreeLeaf):
            path_nodes.append(current_node)
            left_child = right_child = None

            if isinstance(current_node, TreeNode):
                if current_node.left_hash:
                    left_child = self.get_node_by_hash(current_node.left_hash)
                if current_node.right_hash:
                    right_child = self.get_node_by_hash(current_node.right_hash)

            if lookup_key < current_node.pivot_prefix:
                current_node = left_child
                if right_child is not None:
                    closure_nodes.append(right_child)
            else:
                current_node = right_child
                if left_child is not None:
                    closure_nodes.append(left_child)

        path_nodes.append(current_node)
        return path_nodes, closure_nodes

    def __contains__(self, lookup_key):
        """Check if lookup key is in the tree."""
        try:
            self.__getitem__(lookup_key)
            return True
        except KeyError:
            return False

    def __getitem__(self, lookup_key):
        """Retrieve the value for a given lookup key."""
        path, closure = self.get_inclusion_proof(lookup_key)
        if path:
            leaf_node = path[-1]
            if leaf_node.lookup_key == lookup_key:
                serialized_payload = self.object_store.get(
                        leaf_node.payload_hash)
                return serialized_payload

        raise KeyError('The item with given lookup key was not found.')

    @property
    def root_node(self):
        """The root node."""
        return self.get_node_by_hash(self.root)

    def __repr__(self):
        return ('Tree('
                'object_store={self.object_store}, '
                'root=\'{self.root}\')').format(
                    self=self)


class TreeBuilder(object):
    """Builder for a key-value Merkle tree.

    :param object_store: Object store
    :param items: Dictionary of items to be committed to a tree.

    You can add items using a dict-like interface:

    >>> from .store import Sha256DictStore
    >>> store = Sha256DictStore()
    >>> builder = TreeBuilder(store)
    >>> builder['foo'] = b'bar'
    >>> builder['baz'] = b'zez'
    >>> tree = builder.commit()
    >>> 'foo' in tree
    True
    """

    def __init__(self, object_store):
        self.object_store = object_store
        self.items = {}

    def __setitem__(self, lookup_key, value):
        """Add item for committing to the tree."""
        self.items[lookup_key] = value

    def _make_subtree(self, items):
        """Build a tree from sorted items.

        :param items: An iterable of comparable and serializable items
        """

        if len(items) == 0:
            raise ValueError("No items to put.")

        if len(items) == 1:
            (key, serialized_obj), = items
            value_hash = self.object_store.hash_object(serialized_obj)
            leaf = TreeLeaf(lookup_key=key, payload_hash=value_hash)
            return [leaf]

        else:
            middle = len(items) // 2
            pivot_prefix, pivot_obj = items[middle]
            left_partition = items[:middle]
            # NOTE: The right partition includes the pivot node
            right_partition = items[middle:]
            left_subtree_nodes = self._make_subtree(left_partition)
            right_subtree_nodes = self._make_subtree(right_partition)

            # Compute minimal lookup prefixes
            pivot_prefixes = [pivot_prefix]
            left_child = left_subtree_nodes[0]
            right_child = right_subtree_nodes[0]

            def get_node_key(node):
                if isinstance(node, TreeLeaf):
                    return node.lookup_key
                elif isinstance(node, TreeNode):
                    return node.pivot_prefix

            if left_subtree_nodes:
                pivot_prefixes.append(get_node_key(left_child))
            if right_subtree_nodes:
                pivot_prefixes.append(get_node_key(right_child))
            common_prefix = os.path.commonprefix(pivot_prefixes)
            pivot_prefix = pivot_prefix[:max(1, len(common_prefix) + 1)]

            # Compute hashes of direct children.
            left_hash = None
            right_hash = None
            left_hash=self.object_store.hash_object(
                encode(left_subtree_nodes[0]))
            right_hash=self.object_store.hash_object(
                encode(right_subtree_nodes[0]))

            pivot_node = TreeNode(pivot_prefix=pivot_prefix,
                    left_hash=left_hash, right_hash=right_hash)

            return [pivot_node] + left_subtree_nodes + right_subtree_nodes

    # TODO: Figure out if we can have this as an atomic transaction
    def commit(self):
        """Commit items to the tree."""
        items = sorted(self.items.items(), key=lambda t: t[0])
        nodes = self._make_subtree(items)

        # Put intermediate nodes into the store.
        for node in nodes:
            serialized_node = encode(node)
            self.object_store.add(serialized_node)

        # Put items themselves into the store.
        for serialized_obj in self.items.values():
            self.object_store.add(serialized_obj)

        root_node = nodes[0]
        root = self.object_store.hash_object(encode(root_node))
        return Tree(self.object_store, root)

    def __repr__(self):
        return ('TreeBuilder('
                'object_store={self.object_store}, '
                'items={self.items})').format(
                    self=self)

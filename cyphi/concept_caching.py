#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Concept Caching
~~~~~~~~~~~~~~~

Objects and functions for managing the normalization, caching, and retrieval of
concepts.
"""

from collections import namedtuple
import numpy as np
from marbl import MarblSet

from . import utils, models, db
from .network import Network
from .constants import DIRECTIONS, PAST, FUTURE


# A simple container for Mice data without the nested Mip structure.
NormalizedMice = namedtuple('NormalizedMice', ['phi', 'direction', 'mechanism',
                                               'purview', 'repertoire'])


class NormalizedMechanism:

    """A mechanism rendered into a normal form, suitable for use as a cache key
    in concept memoization.


    - get the set of all nodes that input to (output from) at least one mechanism node
    - sort the marbls in a stable way (this is done on initializing the marblset)
    - iterate over sorted marbls; for each one, iterate over its corresponding
    -     mechanism node's inputs (outputs)
    - label each input (output) with a unique integer
    - these are the "normalized indices" of the inputs (outputs)
    - record the inverse mapping, that sends a normal index to a real index

    two normalized mechanisms are the same if they have the same marblset,
        inputs, and outputs.



    Attributes:
        marblset:
        normalized_indices:
        unnormalized_indices:
        inputs:
        outputs:
    """

    # NOTE: We use lists and indices throughout, instead of dictionaries (which
    # would perhaps be more elegant), to avoid repeatedly computing the hash of
    # the marbls.
    def __init__(self, mechanism, cut, normalize_tpms=True):
        self.indices = utils.nodes2indices(mechanism)
        # Apply the cut to the network and get the MarblSet from its nodes.
        # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        # Get the ps
        net = mechanism[0].network
        # Apply the cut to the network's connectivity matrix.
        cut_cm = utils.apply_cut(cut, net.connectivity_matrix)
        # Make a new network with the cut applied.
        cut_network = Network(net.tpm, net.current_state, net.past_state,
                                connectivity_matrix=cut_cm)
        # Get the nodes in the mechanism with the cut applied.
        cut_mechanism = tuple(cut_network.nodes[i] for i in
                              self.indices)
        # Grab the marbls from the cut-network nodes.
        marbls = [(n.marbl if normalize_tpms else n.raw_marbl)
                  for n in cut_mechanism]
        # Normalize the cut mechanism as a MarblSet.
        self.marblset = MarblSet(marbls)
        M = range(len(self.marblset))
        # Associate marbls in the marblset to the mechanism nodes they were
        # generated from.
        marbl_preimage = [
            # The ith marbl corresponds to the jth node in the mechanism, where
            # j is the image of i under the marblset's permutation.
            cut_mechanism[self.marblset.permutation[i]]
            for i, marbl in enumerate(self.marblset)
        ]
        # Associate each marbl to the inputs of its preimage node.
        io = {
            # Inputs
            DIRECTIONS[PAST]: [
                marbl_preimage[m].inputs for m in M],
            # Outputs
            DIRECTIONS[FUTURE]: [
                marbl_preimage[m].outputs for m in M]
        }
        # Now, we generate the normalized index of each node that inputs
        # (outputs) to at least one mechanism node. Also record the reverse
        # mapping, that sends normalized indices to the original indices.
        #
        # This is done by iterating through the marbls' inputs (outputs) and
        # assigning each new node we encounter a unique integer label.
        #
        # So, for example, if the preimage nodes of the first and second marbl
        # share an input node i, node i will be assigned a label only once,
        # when the first marbl's inputs are encountered. When that node i is
        # encountered again while iterating through the inputs of the second
        # marbl's preimage, it will not be assigned a new label.
        # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        # This will hold a mapping from the indices of the nodes of the inputs
        # to the mechanism that the NormalizedMechanism was initialized with to
        # their normalized indices.
        self.normalized_indices = {
            DIRECTIONS[PAST]: {},
            DIRECTIONS[FUTURE]: {}
        }
        counter = {DIRECTIONS[PAST]: 0, DIRECTIONS[FUTURE]: 0}
        for d in DIRECTIONS:
            for m in M:
                # Assign each of the marbl's inputs (outputs) a label if it
                # hasn't been labeled already.
                for node in io[d][m]:
                    if node.index not in self.normalized_indices[d]:
                        # Assign the next unused integer as the label.
                        self.normalized_indices[d][node.index] = counter[d]
                        # Increment the counter so the next label is different.
                        counter[d] += 1
        # Get the inverse mappings.
        self.unnormalized_indices = {
            DIRECTIONS[PAST]: {
                v: k for k, v in
                self.normalized_indices[DIRECTIONS[PAST]].items()},
            DIRECTIONS[FUTURE]: {
                v: k for k, v in
                self.normalized_indices[DIRECTIONS[FUTURE]].items()}
        }
        # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        # Associate each marbl with its normally-labeled inputs.
        # This captures the interrelationships between the mechanism nodes in a
        # stable way.
        self.inputs = tuple(
            tuple(self.normalized_indices[DIRECTIONS[PAST]][n.index]
                  for n in io[DIRECTIONS[PAST]][m])
            for m in M)
        self.outputs = tuple(
            tuple(self.normalized_indices[DIRECTIONS[FUTURE]][n.index]
                  for n in io[DIRECTIONS[FUTURE]][m])
            for m in M)

    @property
    def permutation(self):
        """The permutation that maps mechanism nodes to the position of their
        marbl in the marblset."""
        return self.marblset.permutation

    # TODO!!!: make hash independent of python
    def __hash__(self):
        return hash((self.marblset, self.inputs, self.outputs))

    def __str__(self):
        return str(self.indices)

    def __repr__(self):
        return str(self)


def _normalize_purview_and_repertoire(purview, repertoire, normalized_indices):
    """Return a normalized purview and repertoire.

    A normalized purview is a tuple of the normalized indices of its nodes, and
    a normalized repertoire is obtained by squeezing and then reordering its
    dimensions so they correspond to the normalized purview.
    """
    # Get the normalized indices of the purview nodes.
    normalized_purview = tuple(normalized_indices[n.index] for n in purview)
    # If the repertoire is None, from a null MIP, return immediately.
    if repertoire is None:
        return normalized_purview, repertoire
    # Get the permutation of the purview nodes that sends them to the sorted
    # list of their normalized indices.
    L = [(normalized_purview[i], i) for i in range(len(normalized_purview))]
    L.sort()
    _, permutation = zip(*L)
    # Permute and squeeze the dimensions of the repertoire so they are ordered
    # by their corresponding purview nodes' normalized indices.
    normalized_repertoire = repertoire.squeeze().transpose(permutation)
    # Return the normalized purview and repertoire.
    return normalized_purview, normalized_repertoire


def _normalize_mice(direction, mice, normalized_mechanism):
    normalized_indices = normalized_mechanism.normalized_indices[direction]
    # Get the normalized purview and repertoire
    purview, repertoire = _normalize_purview_and_repertoire(
        mice.purview, mice.repertoire, normalized_indices)
    return NormalizedMice(
        phi=mice.phi,
        direction=mice.direction,
        mechanism=mice.mechanism,
        purview=purview,
        repertoire=repertoire)


class NormalizedConcept:

    """A precomputed concept in a form suitable for memoization.

    Attributes:
        mechanism (NormalizedMechanism):
        phi (float):
        cause (NormalizedMice):
        effect (NormalizedMice):
    """

    # TODO put in phi values

    def __init__(self, normalized_mechanism, concept):
        """Upon initialization, the normal form of the concept to be cached is
        computed, and data relating its cause and effect purviews are stored
        that allows the concept to be reconstituted in a different network."""
        self.mechanism = normalized_mechanism
        self.phi = concept.phi
        self.cause = _normalize_mice(DIRECTIONS[PAST], concept.cause,
                                     self.mechanism)
        self.effect = _normalize_mice(DIRECTIONS[FUTURE], concept.effect,
                                      self.mechanism)

    def __hash__(self):
        return hash(self.mechanism)

    def __str__(self):
        return str(self.mechanism.indices)

    def __repr__(self):
        return str(self)


def _unnormalize_purview_and_repertoire(normalized_purview,
                                        normalized_repertoire,
                                        unnormalized_indices,
                                        network):
    # Get the unnormalized purview indices.
    purview_indices = tuple(unnormalized_indices[normal_index]
                            for normal_index in normalized_purview)
    # Get the actual purview nodes.
    purview = network.indices2nodes(purview_indices)
    # If the normalized repertoire is None, from a null MIP, return
    # immediately.
    if normalized_repertoire is None:
        return purview, normalized_repertoire
    # Expand the repertoire's dimensions to fit the network dimensionality.
    new_shape = (normalized_repertoire.shape +
                 tuple([1] * (network.size - normalized_repertoire.ndim)))
    repertoire = normalized_repertoire.reshape(new_shape)
    # Get the permutation that sends the normalized repertoire's non-singleton
    # dimensions to the dimensions corresponding to the indices of the
    # unnormalized purview nodes.
    permutation = (purview_indices +
                   tuple(set(range(network.size)) - set(purview_indices)))
    # np.transpose actually takes the inverse permutation, so invert it.
    permutation = np.arange(network.size)[np.argsort(permutation)]
    # Permute the repertoires dimensions so they correspond to the unnormalized
    # purview.
    repertoire = repertoire.transpose(permutation)
    return purview, repertoire


def _unnormalize_mice(normalized_mice, normalized_mechanism, network):
    """Convert a normalized MICE to its proper representation in the context of
    a network.

    .. warning::
        Information about the underlying MIP's partition is lost during
        normalization since it is dependent on the specific structure of the
        network in which the MIP was computed. Thus, the underlying MIPs of
        unnormalized MICE have no parition or partitioned repertoire.
    """
    # Get the unnormalized purview and repertoire.
    purview, repertoire = _unnormalize_purview_and_repertoire(
        normalized_mice.purview,
        normalized_mice.repertoire,
        normalized_mechanism.unnormalized_indices[normalized_mice.direction],
        network)
    return models.Mice(models.Mip(
        phi=normalized_mice.phi,
        direction=normalized_mice.direction,
        mechanism=normalized_mice.mechanism,
        purview=purview,
        unpartitioned_repertoire=repertoire,
        # Information about the partition is lost during normalization.
        partitioned_repertoire=None,
        partition=None
    ))


def _unnormalize(normalized_concept, normalized_mechanism, mechanism, network):
    """Convert a normalized concept to its proper representation in the context
    of the given network."""
    cause=_unnormalize_mice(normalized_concept.cause,
                            normalized_mechanism,
                            network)
    effect=_unnormalize_mice(normalized_concept.effect,
                                normalized_mechanism,
                                network)
    return models.Concept(
        phi=normalized_concept.phi,
        mechanism=mechanism,
        cause=cause,
        effect=effect)


def _get(raw, normalized_mechanism, mechanism, subsystem):
    """Get a normalized concept from the database and unnormalize it before
    returning it."""
    key = db.generate_key(normalized_mechanism)
    normalized_concept = db.get(key)
    if normalized_concept is None:
        return None
    concept = _unnormalize(normalized_concept, normalized_mechanism, mechanism,
                           subsystem.network)
    return concept


def _set(normalized_mechanism, concept):
    """Normalize and store a concept with a normalized mechanism as the key."""
    key = db.generate_key(normalized_mechanism)
    value = NormalizedConcept(normalized_mechanism, concept)
    return db.set(key, value)


def concept(subsystem, mechanism, cut=None):
    if cut is None:
        cut = subsystem.null_cut
    # First we try to retrieve the concept without normalizing TPMs, which is
    # expensive.
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    raw_normalized_mechanism = NormalizedMechanism(mechanism, cut,
                                                   normalize_tpms=False)
    # See if we have a precomputed value without normalization.
    cached_concept = _get(True, raw_normalized_mechanism, mechanism, subsystem)
    if cached_concept is not None:
        return cached_concept
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    # We didn't find a precomputed concept with the raw normalized TPM, so now
    # we normalize TPMs as well.
    normalized_mechanism = NormalizedMechanism(mechanism, cut)
    # Try to retrieve the concept with the fully-normalized mechanism.
    cached_concept = _get(False, normalized_mechanism, mechanism, subsystem)
    if cached_concept is not None:
        return cached_concept
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    # We didn't find any precomputed concept at all, so compute it, and store
    # the result with the raw normalized mechanism and the fully-normalized
    # mechanism as keys.
    concept = subsystem.concept(mechanism, cut)
    _set(raw_normalized_mechanism, concept)
    _set(normalized_mechanism, concept)
    return concept
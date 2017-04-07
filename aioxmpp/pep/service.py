########################################################################
# File name: service.py
# This file is part of: aioxmpp
#
# LICENSE
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this program.  If not, see
# <http://www.gnu.org/licenses/>.
#
########################################################################
import asyncio
import contextlib
import weakref

import aioxmpp
import aioxmpp.service as service
import aioxmpp.callbacks as callbacks


class PEPClient(service.Service):
    """
    :class:`PEPClient` simplifies working with PEP services.

    Compared to :class:`~aioxmpp.PubSubClient` it supports automatic
    checking for server support, a stream-lined API. It is intended to
    make PEP things easy. If you need more fine-grained control or do
    things which are not usually handled by the defaults when using PEP, use
    :class:`~aioxmpp.PubSubClient` directly.

    See :class:`register_pep_node` for the high-level interface for
    claiming a PEP node and receiving event notifications.

    There also is a low-level interface for claiming nodes:

    .. automethod:: is_claimed

    .. automethod:: claim_pep_node

    Further we have a convenience method for publishing items in the client's
    PEP service:

    .. automethod:: publish

    Use the :class:`aioxmpp:PubSubClient` For explicit subscription
    and unsubscription .
    """
    ORDER_AFTER = [
        aioxmpp.DiscoClient,
        aioxmpp.DiscoServer,
        aioxmpp.PubSubClient,
    ]

    def __init__(self, client, **kwargs):
        super().__init__(client, **kwargs)
        self._pubsub = self.dependencies[aioxmpp.PubSubClient]
        self._disco_client = self.dependencies[aioxmpp.DiscoClient]
        self._disco_server = self.dependencies[aioxmpp.DiscoServer]

        self._pep_node_claims = weakref.WeakValueDictionary()

    def is_claimed(self, node):
        """
        Return whether `node` is claimed.
        """
        return node in self._pep_node_claims

    def claim_pep_node(self, node_namespace, *,
                       register_feature=True, notify=False):
        """
        Claim node `node_namespace`.

        :param node_namespace: the pubsub node whose events shall be
            handled.
        :param handler: the handler to install.
        :type handler: callable, see
            :attr:`aioxmpp.PubSubClient.on_item_publish`
            for the arguments.
        :param register_feature: Whether to publish the `node_namespace`
            as feature.
        :param notify: Whether to register the ``+notify`` feature to
            receive notification without explicit subscription.
        :raises RuntimeError: if a handler for `node_namespace` is already
            set.
        :returns: a :class:`RegisteredPEPNode` instance representing
            the claim.

        This registers `node_namespace` as feature for service discovery
        unless ``register_feature=False`` is passed.
        """
        if node_namespace in self._pep_node_claims:
            raise RuntimeError(
                "claiming already claimed node"
            )
        registered_node = RegisteredPEPNode(
            self,
            node_namespace,
            register_feature=register_feature,
            notify=notify,
        )

        finalizer = weakref.finalize(
            registered_node,
            weakref.WeakMethod(registered_node._unregister)
        )
        # we cannot guarantee that disco is not cleared up already
        finalizer.atexit = False
        self._pep_node_claims[node_namespace] = registered_node

        return registered_node

    def _unclaim(self, node_namespace):
        self._pep_node_claims.pop(node_namespace)

    @asyncio.coroutine
    def _check_for_pep(self):
        # XXX: should this be done when the stream connects
        # and we use the cached result later on (i.e. disable
        # the PEP service if the server does not support PEP)
        disco_info = yield from self._disco_client.query_info(
            self.client.local_jid.bare()
        )

        for item in disco_info.identities.filter(attrs={"category": "pubsub"}):
            if item.type_ == "pep":
                break
        else:
            raise RuntimeError("server does not support PEP")

    @service.depsignal(aioxmpp.PubSubClient, "on_item_published")
    def _handle_pubsub_publish(self, jid, node, item, *, message=None):
        try:
            registered_node = self._pep_node_claims[node]
        except KeyError:
            return

        # TODO: handle empty payloads due to (mis-)configuration of
        # the node specially.
        registered_node.on_item_publish(jid, node, item, message=message)

    def publish(self, node, data, *, id_=None):
        """
        Publish an item `data` in the PubSub node `node` on the
        PEP service associated with the user's JID.

        :param node: The PubSub node to publish to.
        :param data: The item to publish.
        :type data: An XSO representing the paylaod.
        :param id_: The id the published item shall have.
        :returns: The PubSub id of the published item or
            :data:`None` if it is unknown.

        If no `id_` is given it is generated by the server (and may be
        returned).
        """
        yield from self._check_for_pep()
        return (yield from self._pubsub.publish(None, node, data, id_=id_))


class RegisteredPEPNode:
    """
    Handle for registered PEP nodes.

    You have to keep a reference to :class:`RegisteredPEPNode` to
    uphold the claim, when a :class:`RegisteredPEPNode` is garbage
    collected it is closed automatically. It is not enough to have a
    callback registered! It is strongly recommended to explicitly
    close the registered node if it is no longer needed or to use the
    :class:`register_pep_node` descriptor for automatic life-cycle
    handling.

    .. signal:: on_item_publish(jid, node, item, message=None)

       Fires when an event is received for this PEP node. The arguments
       are as for :attr:`aioxmpp.PubSubClient.on_item_publish`.

    .. autoattribute:: notify

    .. autoattribute:: registered_feature

    .. automethod:: close

    """

    def __init__(self, pep_service, node, register_feature, notify):
        self._pep_service = pep_service
        self._node = node
        self._feature_registered = register_feature
        self._notify = notify
        self._closed = False

        if self._feature_registered:
            self._register_feature()

        if self._notify:
            self._register_notify()

    on_item_publish = callbacks.Signal()

    def _register_feature(self):
        self._pep_service._disco_server.register_feature(self._node)
        self._feature_registered = True

    def _unregister_feature(self):
        self._pep_service._disco_server.unregister_feature(self._node)
        self._feature_registered = False

    def _register_notify(self):
        self._pep_service._disco_server.register_feature(self._notify_feature)
        self._notify = True

    def _unregister_notify(self):
        self._pep_service._disco_server.unregister_feature(
            self._notify_feature)
        self._notify = False

    def _unregister(self):
        if self._notify:
            self._unregister_notify()

        if self._feature_registered:
            self._unregister_feature()

    def close(self):
        """
        Unclaim the PEP node and unregister the registered features.

        It is not necessary to call close if this claim is managed by
        :class:`register_pep_node`.
        """
        if self._closed:
            return

        self._closed = True
        self._pep_service._unclaim(self.node_namespace)
        self._unregister()

    @property
    def node_namespace(self):
        """The claimed node namespace"""
        return self._node

    @property
    def _notify_feature(self):
        return self._node + "+notify"

    @property
    def notify(self):
        """
        Whether we have enabled the ``+notify`` feature to automatically
        receive notifications.

        When setting this property the feature is registered and
        unregistered appropriately.
        """
        return self._notify

    @notify.setter
    def notify(self, value):
        if self._closed:
            raise RuntimeError(
                "modifying a closed RegisteredPEPNode is forbidden"
            )
        # XXX: do we want to do strict type checking here?
        if (not value) == self._notify:
            if self._notify:
                self._unregister_notify()
            else:
                self._register_notify()

    @property
    def feature_registered(self):
        """
        Whether we have registered the node namespace as feature.

        When setting this property the feature is registered and
        unregistered appropriately.
        """
        return self._feature_registered

    @feature_registered.setter
    def feature_registered(self, value):
        if self._closed:
            raise RuntimeError(
                "modifying a closed RegisteredPEPNode is forbidden"
            )
        # XXX: do we want to do strict type checking here?
        if (not value) == self._feature_registered:
            if self._feature_registered:
                self._unregister_feature()
            else:
                self._register_feature()


class register_pep_node(service.Descriptor):
    """Service descriptor claiming a PEP node.

    :param node_namespace: The PubSub payload namespace to handle.
    :param register_feature: Whether to register the node namespace as feature.
    :param notify: Whether to register for notifications.
    :param max_items: Transparently handle the `max_items` configuration
        option of the PubSub node.

    If `notify` is :data:`True` it registers a ``+notify`` feature,
    for automatic pubsub subscription.

    The value returned by the descriptor is the instance of
    :class:`RegisteredPEPNode` representing the claim.
    """

    def __init__(self, node_namespace, *, register_feature=True,
                 notify=False, max_items=None):
        super().__init__()
        self._node_namespace = node_namespace
        self._notify = notify
        self._register_feature = register_feature
        self._max_items = max_items

    @property
    def node_namespace(self):
        """
        The node namespace to request notifications for.
        """
        return self._node_namespace

    @property
    def register_feature(self):
        """
        Whether we register the node namespace as feature.
        """
        return self._register_feature

    @property
    def notify(self):
        """
        Wether we register the ``+nofity`` feature.
        """
        return self._notify

    @property
    def required_dependencies(self):
        return [PEPClient]

    @contextlib.contextmanager
    def init_cm(self, instance):
        pep_client = instance.dependencies[PEPClient]
        claim = pep_client.claim_pep_node(
            self._node_namespace,
            register_feature=self._register_feature,
            notify=self._notify,
        )
        yield claim
        claim.close()
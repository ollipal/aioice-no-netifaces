import asyncio
import hashlib
import secrets
import socket
import string
from ipaddress import IPv4Address

import netifaces

from . import stun


def candidate_foundation(candidate_type, candidate_transport, base_address):
    """
    See RFC 5245 - 4.1.1.3. Computing Foundations
    """
    key = '%s|%s|%s' % (type, candidate_transport, base_address)
    return hashlib.md5(key.encode('ascii')).hexdigest()


def candidate_priority(candidate_component, candidate_type, local_pref=65535):
    """
    See RFC 5245 - 4.1.2.1. Recommended Formula
    """
    if candidate_type == 'host':
        type_pref = 126
    elif candidate_type == 'prflx':
        type_pref = 110
    elif candidate_type == 'srflx':
        type_pref = 100
    else:
        type_pref = 0

    return (1 << 24) * type_pref + \
           (1 << 8) * local_pref + \
           (256 - candidate_component)


def candidate_pair_priority(local, remote, ice_controlling):
    """
    See RFC 5245 - 5.7.2. Computing Pair Priority and Ordering Pairs
    """
    G = ice_controlling and local.priority or remote.priority
    D = ice_controlling and remote.priority or local.priority
    return (1 << 32) * min(G, D) + 2 * max(G, D) + (G > D and 1 or 0)


def random_string(length):
    allchar = string.ascii_letters + string.digits
    return ''.join(secrets.choice(allchar) for x in range(length))


class Candidate:
    """
    An ICE candidate.
    """
    def __init__(self, foundation, component, transport, priority, host, port,
                 type='host', generation=0):
        self.foundation = foundation
        self.component = component
        self.transport = transport
        self.priority = priority
        self.host = host
        self.port = port
        self.type = type
        self.generation = generation

    def __repr__(self):
        return 'Candidate(%s)' % self

    def __str__(self):
        return '%s %d %s %d %s %d typ %s generation %d' % (
            self.foundation,
            self.component,
            self.transport,
            self.priority,
            self.host,
            self.port,
            self.type,
            self.generation)


class CandidatePair:
    def __init__(self, protocol, remote_candidate):
        self.protocol = protocol
        self.remote_candidate = remote_candidate


def parse_candidate(value):
    bits = value.split()
    return Candidate(
        foundation=bits[0],
        component=int(bits[1]),
        transport=bits[2],
        priority=int(bits[3]),
        host=bits[4],
        port=int(bits[5]),
        type=bits[7],
        generation=int(bits[9]))


class StunProtocol:
    def __init__(self, receiver):
        self.receiver = receiver
        self.transport = None
        self.transactions = {}

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        try:
            message = stun.parse_message(data)
        except ValueError:
            return

        if ((message.message_class == stun.Class.RESPONSE or
             message.message_class == stun.Class.ERROR) and
           message.transaction_id in self.transactions):
            transaction = self.transactions[message.transaction_id]
            transaction.message_received(message, addr)
        self.receiver.stun_message_received(message, addr, self)

    def error_received(self, exc):
        print('Error received:', exc)

    def connection_lost(self, exc):
        print('Socket closed:', exc)

    # custom

    async def request(self, request, addr):
        """
        Execute a STUN transaction and return the response.
        """
        assert request.transaction_id not in self.transactions

        transaction = stun.Transaction(request, addr, self)
        self.transactions[request.transaction_id] = transaction
        response = await transaction.run()
        del self.transactions[request.transaction_id]

        return response

    def send(self, message, addr):
        """
        Send a STUN message.
        """
        self.transport.sendto(bytes(message), addr)


class Component:
    """
    An ICE component.
    """
    def __init__(self, component, addresses, connection):
        self.__addresses = addresses
        self.__connection = connection
        self.__component = component
        self.__protocols = []

    def close(self):
        for protocol in self.__protocols:
            protocol.transport.close()
        self.__protocols = []

    async def get_local_candidates(self):
        candidates = []

        loop = asyncio.get_event_loop()
        for address in self.__addresses:
            # create transport
            _, protocol = await loop.create_datagram_endpoint(
                lambda: StunProtocol(self),
                local_addr=(address, 0))
            port = protocol.transport.get_extra_info('socket').getsockname()[1]
            self.__protocols.append(protocol)

            # add host candidate
            protocol.local_candidate = Candidate(
                foundation=candidate_foundation('host', 'udp', address),
                component=self.__component,
                transport='udp',
                priority=candidate_priority(self.__component, 'host'),
                host=address,
                port=port,
                type='host')
            candidates.append(protocol.local_candidate)

            # query STUN server for server-reflexive candidate
            if self.__connection.stun_server:
                request = stun.Message(message_method=stun.Method.BINDING,
                                       message_class=stun.Class.REQUEST,
                                       transaction_id=random_string(12).encode('ascii'))
                response = await protocol.request(request, self.__connection.stun_server)

                candidates.append(Candidate(
                    foundation=candidate_foundation('srflx', 'udp', address),
                    component=self.__component,
                    transport='udp',
                    priority=candidate_priority(self.__component, 'srflx'),
                    host=str(response.attributes['XOR-MAPPED-ADDRESS'][0]),
                    port=response.attributes['XOR-MAPPED-ADDRESS'][1],
                    type='srflx'))

        self.local_candidates = candidates
        return candidates

    def set_remote_candidates(self, candidates):
        self.remote_candidates = candidates

    def stun_message_received(self, message, addr, protocol):
        if (message.message_method == stun.Method.BINDING and
           message.message_class == stun.Class.REQUEST and
           message.attributes['USERNAME'] == self.__incoming_username()):

            # check for role conflict
            ice_controlling = self.__connection.ice_controlling
            if (ice_controlling and
               ('ICE-CONTROLLING' in message.attributes or 'USE-CANDIDATE' in message.attributes)):
                print("Role conflict, expected to be controlling")
                return
            elif not ice_controlling and 'ICE-CONTROLLED' in message.attributes:
                print("Role conflict, expected to be controlled")
                return

            response = stun.Message(
                message_method=stun.Method.BINDING,
                message_class=stun.Class.RESPONSE,
                transaction_id=message.transaction_id)
            response.attributes['XOR-MAPPED-ADDRESS'] = (IPv4Address(addr[0]), addr[1])
            response.add_message_integrity(self.__connection.local_password.encode('utf8'))
            response.add_fingerprint()
            protocol.send(response, addr)

    async def connect(self):
        # create candidate pairs
        candidate_pairs = []
        for remote_candidate in self.remote_candidates:
            for protocol in self.__protocols:
                pair = CandidatePair(protocol, remote_candidate)
                pair.priority = candidate_pair_priority(protocol.local_candidate,
                                                        remote_candidate,
                                                        self.__connection.ice_controlling)
                candidate_pairs.append(pair)
        candidate_pairs.sort(key=lambda x: -x.priority)

        # perform checks
        for pair in candidate_pairs:
            request = stun.Message(message_method=stun.Method.BINDING,
                                   message_class=stun.Class.REQUEST,
                                   transaction_id=random_string(12).encode('ascii'))
            request.attributes['USERNAME'] = self.__outgoing_username()
            request.attributes['PRIORITY'] = candidate_priority(self.__component, 'prflx')
            if self.__connection.ice_controlling:
                request.attributes['ICE-CONTROLLING'] = self.__connection.tie_breaker
                request.attributes['USE-CANDIDATE'] = None
            else:
                request.attributes['ICE-CONTROLLED'] = self.__connection.tie_breaker
            request.add_message_integrity(self.__connection.remote_password.encode('utf8'))
            request.add_fingerprint()
            await protocol.request(request, (remote_candidate.host, remote_candidate.port))

    def __incoming_username(self):
        return '%s:%s' % (self.__connection.local_username, self.__connection.remote_username)

    def __outgoing_username(self):
        return '%s:%s' % (self.__connection.remote_username, self.__connection.local_username)


class Connection:
    """
    An ICE connection.
    """
    def __init__(self, ice_controlling, stun_server=None):
        self.ice_controlling = ice_controlling
        self.local_username = random_string(4)
        self.local_password = random_string(22)
        self.remote_username = None
        self.remote_password = None
        self.stun_server = stun_server
        self.tie_breaker = secrets.token_bytes(8)

        # get host addresses
        addresses = []
        for interface in netifaces.interfaces():
            for address in netifaces.ifaddresses(interface)[socket.AF_INET]:
                if address['addr'] != '127.0.0.1':
                    addresses.append(address['addr'])

        self.__component = Component(1, addresses, self)

    async def get_local_candidates(self):
        """
        Gather local candidates.
        """
        return await self.__component.get_local_candidates()

    def set_remote_candidates(self, candidates):
        """
        Set remote candidates.
        """
        self.__component.set_remote_candidates(candidates)

    async def connect(self):
        """
        Perform ICE handshake.
        """
        await self.__component.connect()

    def close(self):
        """
        Close the connection.
        """
        self.__component.close()
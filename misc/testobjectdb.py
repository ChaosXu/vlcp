'''
Created on 2016/3/31

:author: hubo
'''
from __future__ import print_function, absolute_import, division
from vlcp.utils.dataobject import DataObject, DataObjectSet, updater, DataObjectUpdateEvent,\
    multiwaitif, dump, set_new, ReferenceObject, request_context, Relationship
from vlcp.server.module import depend, Module, call_api, ModuleLoadStateChanged,\
    api
import vlcp.service.kvdb.objectdb as objectdb
from vlcp.config.config import defaultconfig
from vlcp.event.runnable import RoutineContainer, RoutineException
from uuid import uuid1
from vlcp.server import main
import logging
from functools import partial

class PhysicalNetwork(DataObject):
    _prefix = 'vlcptest.physicalnetwork'
    _indices = ('id',)

class LogicalNetwork(DataObject):
    _prefix = 'vlcptest.logicalnetwork'
    _indices = ('id',)

class PhysicalNetworkMap(DataObject):
    _prefix = 'vlcptest.physicalnetworkmap'
    _indices = ('id',)
    def __init__(self, prefix=None, deleted=False):
        DataObject.__init__(self, prefix=prefix, deleted=deleted)
        self.networks = DataObjectSet()
        self.network_allocation = dict()
        self.ports = DataObjectSet()


PhysicalNetworkMap._network = Relationship(PhysicalNetworkMap, PhysicalNetwork, ('id', 'id'))


class LogicalNetworkMap(DataObject):
    _prefix = 'vlcptest.logicalnetworkmap'
    _indices = ('id',)
    def __init__(self, prefix=None, deleted=False):
        DataObject.__init__(self, prefix=prefix, deleted=deleted)
        self.ports = DataObjectSet()


LogicalNetworkMap._network = Relationship(LogicalNetworkMap, LogicalNetwork, ('id', 'id'))


class PhysicalPort(DataObject):
    _prefix = 'vlcptest.physicalport'
    _indices = ('systemid', 'bridge', 'name')

class LogicalPort(DataObject):
    _prefix = 'vlcptest.logicalport'
    _indices = ('id',)

class PhysicalNetworkSet(DataObject):
    _prefix = 'vlcptest.physicalnetworkset'

class LogicalNetworkSet(DataObject):
    _prefix = 'vlcptest.logicalnetworkset'

class LogicalPortSet(DataObject):
    _prefix = 'vlcptest.logicalportset'
    
class PhysicalPortSet(DataObject):
    _prefix = 'vlcptest.physicalportset'

@defaultconfig
@depend(objectdb.ObjectDB)
class TestObjectDB(Module):
    def __init__(self, server):
        Module.__init__(self, server)
        self.apiroutine = RoutineContainer(self.scheduler)
        self.apiroutine.main = self._main
        self.routines.append(self.apiroutine)
        self._reqid = 0
        self._ownerid = uuid1().hex
        self.createAPI(api(self.createlogicalnetwork, self.apiroutine),
                       api(self.createlogicalnetworks, self.apiroutine),
                       api(self.createphysicalnetwork, self.apiroutine),
                       api(self.createphysicalnetworks, self.apiroutine),
                       api(self.createphysicalport, self.apiroutine),
                       api(self.createphysicalports, self.apiroutine),
                       api(self.createlogicalport, self.apiroutine),
                       api(self.createlogicalports, self.apiroutine),
                       api(self.getlogicalnetworks, self.apiroutine))
        self._logger.setLevel(logging.DEBUG)

    async def _monitor(self):
        update_event = DataObjectUpdateEvent.createMatcher()
        while True:
            ev = await update_event
            self._logger.info('Database update: %r', ev)

    async def _dumpkeys(self, keys):
        self._reqid += 1
        reqid = ('testobjectdb', self._reqid)
        with request_context(reqid, self.apiroutine):
            retobjs = await call_api(self.apiroutine, 'objectdb', 'mget', {'keys': keys, 'requestid': reqid})
            return [dump(v) for v in retobjs]

    async def _updateport(self, key):
        unload_matcher = ModuleLoadStateChanged.createMatcher(self.target, ModuleLoadStateChanged.UNLOADING)
        async def updateinner():
            self._reqid += 1
            reqid = ('testobjectdb', self._reqid)
            with request_context(reqid, self.apiroutine):
                portobj = await call_api(self.apiroutine, 'objectdb', 'get', {'key': key, 'requestid': reqid})
                if portobj is not None:
                    @updater
                    def write_status(portobj):
                        if portobj is None:
                            raise ValueError('Already deleted')
                        if not hasattr(portobj, 'owner'):
                            portobj.owner = self._ownerid
                            portobj.status = 'READY'
                            return [portobj]
                        else:
                            raise ValueError('Already managed')
                    try:
                        await call_api(self.apiroutine, 'objectdb', 'transact', {'keys': [portobj.getkey()], 'updater': write_status})
                    except ValueError:
                        pass
                    else:
                        await portobj.waitif(self.apiroutine, lambda x: x.isdeleted() or hasattr(x, 'owner'))
                        self._logger.info('Port managed: %r', dump(portobj))
                        while True:
                            await portobj.waitif(self.apiroutine, lambda x: True, True)
                            if portobj.isdeleted():
                                self._logger.info('Port deleted: %r', dump(portobj))
                                break
                            else:
                                self._logger.info('Port updated: %r', dump(portobj))
        try:
            await self.apiroutine.withException(updateinner(), unload_matcher)
        except RoutineException:
            pass

    async def _waitforchange(self, key):
        with request_context('testobjectdb', self.apiroutine):
            setobj = await call_api(self.apiroutine, 'objectdb', 'watch', {'key': key, 'requestid': 'testobjectdb'})
            await setobj.wait(self.apiroutine)
            oldset = set()
            while True:
                for weakref in setobj.set.dataset().difference(oldset):
                    self.apiroutine.subroutine(self._updateport(weakref.getkey()))
                oldset = set(setobj.set.dataset())
                await setobj.waitif(self.apiroutine, lambda x: not x.isdeleted(), True)

    async def _main(self):
        routines = []
        routines.append(self._monitor())
        keys = [LogicalPortSet.default_key(), PhysicalPortSet.default_key()]
        for k in keys:
            routines.append(self._waitforchange(k))
        await self.apiroutine.execute_all(routines)

    async def load(self, container):
        @updater
        def initialize(phynetset, lognetset, logportset, phyportset):
            if phynetset is None:
                phynetset = PhysicalNetworkSet()
                phynetset.set = DataObjectSet()
            if lognetset is None:
                lognetset = LogicalNetworkSet()
                lognetset.set = DataObjectSet()
            if logportset is None:
                logportset = LogicalPortSet()
                logportset.set = DataObjectSet()
            if phyportset is None:
                phyportset = PhysicalPortSet()
                phyportset.set = DataObjectSet()
            return [phynetset, lognetset, logportset, phyportset]
        await call_api(container, 'objectdb', 'transact', {'keys':[PhysicalNetworkSet.default_key(),
                                                                   LogicalNetworkSet.default_key(),
                                                                   LogicalPortSet.default_key(),
                                                                   PhysicalPortSet.default_key()],
                                                             'updater': initialize})
        await Module.load(self, container)

    async def createphysicalnetwork(self, type = 'vlan', id = None, **kwargs):
        new_network, new_map = self._createphysicalnetwork(type, id, **kwargs)
        @updater
        def create_phy(physet, phynet, phymap):
            phynet = set_new(phynet, new_network)
            phymap = set_new(phymap, new_map)
            physet.set.dataset().add(phynet.create_weakreference())
            return [physet, phynet, phymap]
        await call_api(self.apiroutine, 'objectdb', 'transact', {'keys':[PhysicalNetworkSet.default_key(),
                                                                           new_network.getkey(),
                                                                           new_map.getkey()],'updater':create_phy})
        return (await self._dumpkeys([new_network.getkey()]))[0]

    async def createphysicalnetworks(self, networks):
        new_networks = [self._createphysicalnetwork(**n) for n in networks]
        @updater
        def create_phys(physet, *phynets):
            return_nets = [None, None] * len(new_networks)
            for i in range(0, len(new_networks)):
                return_nets[i * 2] = set_new(phynets[i * 2], new_networks[i][0])
                return_nets[i * 2 + 1] = set_new(phynets[i * 2 + 1], new_networks[i][1])
                physet.set.dataset().add(new_networks[i][0].create_weakreference())
            return [physet] + return_nets
        keys = [sn.getkey() for n in new_networks for sn in n]
        await call_api(self.apiroutine, 'objectdb', 'transact', {'keys':[PhysicalNetworkSet.default_key()] + keys,'updater':create_phys})
        return await self._dumpkeys([n[0].getkey() for n in new_networks])

    def _createlogicalnetwork(self, physicalnetwork, id = None, **kwargs):
        if not id:
            id = str(uuid1())
        new_network = LogicalNetwork.create_instance(id)
        for k,v in kwargs.items():
            setattr(new_network, k, v)
        new_network.physicalnetwork = ReferenceObject(PhysicalNetwork.default_key(physicalnetwork))
        new_networkmap = LogicalNetworkMap.create_instance(id)
        new_networkmap.network = new_network.create_reference()
        return new_network,new_networkmap

    async def createlogicalnetworks(self, networks):
        new_networks = [self._createlogicalnetwork(**n) for n in networks]
        physical_networks = list(set(n[0].physicalnetwork.getkey() for n in new_networks))
        physical_maps = [PhysicalNetworkMap.default_key(PhysicalNetwork._getIndices(k)[1][0]) for k in physical_networks]
        @updater
        def create_logs(logset, *networks):
            phy_maps = list(networks[len(new_networks) * 2 : len(new_networks) * 2 + len(physical_networks)])
            phy_nets = list(networks[len(new_networks) * 2 + len(physical_networks):])
            phy_dict = dict(zip(physical_networks, zip(phy_nets, phy_maps)))
            return_nets = [None, None] * len(new_networks)
            for i in range(0, len(new_networks)):
                return_nets[2 * i] = set_new(networks[2 * i], new_networks[i][0])
                return_nets[2 * i + 1] = set_new(networks[2 * i + 1], new_networks[i][1])
            for n in return_nets[::2]:
                phynet, phymap = phy_dict.get(n.physicalnetwork.getkey())
                if phynet is None:
                    _, (phyid,) = PhysicalNetwork._getIndices(n.physicalnetwork.getkey())
                    raise ValueError('Physical network %r does not exist' % (phyid,))
                else:
                    if phynet.type == 'vlan':
                        if hasattr(n, 'vlanid'):
                            n.vlanid = int(n.vlanid)
                            if n.vlanid <= 0 or n.vlanid >= 4095:
                                raise ValueError('Invalid VLAN ID')
                            # VLAN id is specified
                            if str(n.vlanid) in phymap.network_allocation:
                                raise ValueError('VLAN ID %r is already allocated in physical network %r' % (n.vlanid,phynet.id))
                            else:
                                for start,end in phynet.vlanrange:
                                    if start <= n.vlanid <= end:
                                        break
                                else:
                                    raise ValueError('VLAN ID %r is not in vlan range of physical network %r' % (n.vlanid,phynet.id))
                            phymap.network_allocation[str(n.vlanid)] = n.create_weakreference()
                        else:
                            # Allocate a new VLAN id
                            for start,end in phynet.vlanrange:
                                for vlanid in range(start, end + 1):
                                    if str(vlanid) not in phymap.network_allocation:
                                        break
                                else:
                                    continue
                                break
                            else:
                                raise ValueError('Not enough VLAN ID to be allocated in physical network %r' % (phynet.id,))
                            n.vlanid = vlanid
                            phymap.network_allocation[str(vlanid)] = n.create_weakreference()
                    else:
                        if phymap.network_allocation:
                            raise ValueError('Physical network %r is already allocated by another logical network', (phynet.id,))
                        phymap.network_allocation['native'] = n.create_weakreference()
                    phymap.networks.dataset().add(n.create_weakreference())
                logset.set.dataset().add(n.create_weakreference())
            return [logset] + return_nets + phy_maps
        await call_api(self.apiroutine, 'objectdb', 'transact', {'keys': [LogicalNetworkSet.default_key()] +\
                                                                            [sn.getkey() for n in new_networks for sn in n] +\
                                                                            physical_maps +\
                                                                            physical_networks,
                                                                   'updater': create_logs})
        return await self._dumpkeys([n[0].getkey() for n in new_networks])

    async def createlogicalnetwork(self, physicalnetwork, id = None, **kwargs):
        n = {'physicalnetwork':physicalnetwork, 'id':id}
        n.update(kwargs)
        return (await self.createlogicalnetworks([n]))[0]

    def _createphysicalnetwork(self, type = 'vlan', id = None, **kwargs):
        if not id:
            id = str(uuid1())
        if type == 'vlan':
            if 'vlanrange' not in kwargs:
                raise ValueError(r'Must specify vlanrange with network type="vlan"')
            vlanrange = kwargs['vlanrange']
            # Check
            try:
                lastend = 0
                for start, end in vlanrange:
                    if start <= lastend:
                        raise ValueError('VLAN sequences overlapped or disordered')
                    lastend = end
                if lastend >= 4095:
                    raise ValueError('VLAN ID out of range')
            except Exception as exc:
                raise ValueError('vlanrange format error: %s' % (str(exc),))
        else:
            type = 'native'
        new_network = PhysicalNetwork.create_instance(id)
        new_network.type = type
        for k,v in kwargs.items():
            setattr(new_network, k, v)
        new_networkmap = PhysicalNetworkMap.create_instance(id)
        new_networkmap.network = new_network.create_reference()
        return (new_network, new_networkmap)

    async def createphysicalport(self, physicalnetwork, name, systemid = '%', bridge = '%', **kwargs):
        p = {'physicalnetwork':physicalnetwork, 'name':name, 'systemid':systemid,'bridge':bridge}
        p.update(kwargs)
        return (await self.createphysicalports([p]))[0]

    def _createphysicalport(self, physicalnetwork, name, systemid = '%', bridge = '%', **kwargs):
        new_port = PhysicalPort.create_instance(systemid, bridge, name)
        new_port.physicalnetwork = ReferenceObject(PhysicalNetwork.default_key(physicalnetwork))
        for k,v in kwargs.items():
            setattr(new_port, k, v)
        return new_port

    async def createphysicalports(self, ports):
        new_ports = [self._createphysicalport(**p) for p in ports]
        physical_networks = list(set([p.physicalnetwork.getkey() for p in new_ports]))
        physical_maps = [PhysicalNetworkMap.default_key(*PhysicalNetwork._getIndices(k)[1]) for k in physical_networks]
        def _walker(walk, write):
            for p, port in zip(new_ports, ports):
                key = p.getkey()
                try:
                    value = walk(key)
                except KeyError:
                    pass
                else:
                    new_port = self._createphysicalport(**port)
                    value = set_new(value, new_port)
                    try:
                        phynet = walk(new_port.physicalnetwork.getkey())
                    except KeyError:
                        pass
                    else:
                        if phynet is None:
                            _, (phyid,) = PhysicalNetwork._getIndices(p.physicalnetwork.getkey())
                            raise ValueError('Physical network %r does not exist' % (phyid,))
                    write(key, value)
                    try:
                        phymap = walk(PhysicalNetworkMap._network.leftkey(new_port.physicalnetwork))
                    except KeyError:
                        pass
                    else:
                        if phymap is not None:
                            phymap.ports.dataset().add(value.create_weakreference())
                            write(phymap.getkey(), phymap)
                    try:
                        portset = walk(PhysicalPortSet.default_key())
                    except KeyError:
                        pass
                    else:
                        portset.set.dataset().add(value.create_weakreference())
                        write(portset.getkey(), portset)
        
        await call_api(self.apiroutine, 'objectdb', 'writewalk', {'keys': set([PhysicalPortSet.default_key()] +\
                                                                            [p.getkey() for p in new_ports] +\
                                                                            physical_maps +\
                                                                            physical_networks),
                                                                   'walker': _walker})
        return await self._dumpkeys([p.getkey() for p in new_ports])

    async def createlogicalport(self, logicalnetwork, id = None, **kwargs):
        p = {'logicalnetwork':logicalnetwork, 'id':id}
        p.update(kwargs)
        return (await self.createlogicalports([p]))[0]

    def _createlogicalport(self, logicalnetwork, id = None, **kwargs):
        if not id:
            id = str(uuid1())
        new_port = LogicalPort.create_instance(id)
        new_port.logicalnetwork = ReferenceObject(LogicalNetwork.default_key(logicalnetwork))
        for k,v in kwargs.items():
            setattr(new_port, k, v)
        return new_port

    async def createlogicalports(self, ports):
        new_ports = [self._createlogicalport(**p) for p in ports]
        def _walker(walk, write):
            for p in new_ports:
                key = p.getkey()
                try:
                    value = walk(key)
                except KeyError:
                    pass
                else:
                    value = set_new(value, p)
                    try:
                        lognet = walk(value.logicalnetwork.getkey())
                    except KeyError:
                        pass
                    else:
                        if lognet is None:
                            _, (logid,) = LogicalNetwork._getIndices(value.logicalnetwork.getkey())
                            raise ValueError("Logical network %r does not exist" % (logid,))
                    try:
                        logmap = walk(LogicalNetworkMap._network.leftkey(value.logicalnetwork))
                    except KeyError:
                        pass
                    else:
                        if logmap is not None:
                            logmap.ports.dataset().add(value.create_weakreference())
                            write(key, value)
                            write(logmap.getkey(), logmap)
                    try:
                        portset = walk(LogicalPortSet.default_key())
                    except KeyError:
                        pass
                    else:
                        portset.set.dataset().add(value.create_weakreference())
                        write(portset.getkey(), portset)
        keys = set()
        keys.update(p.getkey() for p in new_ports)
        keys.update(p.logicalnetwork.getkey() for p in new_ports)
        keys.update(LogicalNetworkMap._network.leftkey(p.logicalnetwork)
                    for p in new_ports)
        keys.add(LogicalPortSet.default_key())
        await call_api(self.apiroutine, 'objectdb', 'writewalk', {'keys': keys,
                                                                  'walker': _walker})
        return await self._dumpkeys([p.getkey() for p in new_ports])

    async def getlogicalnetworks(self, id = None, physicalnetwork = None, **kwargs):
        def set_walker(key, set, walk, save):
            if set is None:
                return
            for o in set.dataset():
                key = o.getkey()
                try:
                    net = walk(key)
                except KeyError:
                    pass
                else:
                    for k,v in kwargs.items():
                        if getattr(net, k, None) != v:
                            break
                    else:
                        save(key)
        def walker_func(set_func):
            def walker(key, obj, walk, save):
                if obj is None:
                    return
                set_walker(key, set_func(obj), walk, save)
            return walker
        if id is not None:
            self._reqid += 1
            reqid = ('testobjectdb', self._reqid)
            with request_context(reqid, self.apiroutine):
                result = await call_api(self.apiroutine, 'objectdb', 'get', {'key' : LogicalNetwork.default_key(id), 'requestid': reqid})
                if result is None:
                    return []
                if physicalnetwork is not None and physicalnetwork != result.physicalnetwork.id:
                    return []
                for k,v in kwargs.items():
                    if getattr(result, k, None) != v:
                        return []
                return [dump(result)]
        elif physicalnetwork is not None:
            self._reqid += 1
            reqid = ('testobjectdb', self._reqid)
            pm_key = PhysicalNetworkMap.default_key(physicalnetwork)
            with request_context(reqid, self.apiroutine):
                keys, result = await call_api(self.apiroutine, 'objectdb', 'walk', {'keys': [pm_key],
                                                                           'walkerdict': {pm_key: walker_func(lambda x: x.networks)},
                                                                           'requestid': reqid})
                return [dump(r) for r in result]
        else:
            self._reqid += 1
            reqid = ('testobjectdb', self._reqid)
            ns_key = LogicalNetworkSet.default_key()
            with request_context(reqid, self.apiroutine):
                keys, result = await call_api(self.apiroutine, 'objectdb', 'walk', {'keys': [ns_key],
                                                                   'walkerdict': {ns_key: walker_func(lambda x: x.set)},
                                                                   'requestid': reqid})
                return [dump(r) for r in result]

if __name__ == '__main__':
    main("/etc/vlcp.conf", ("__main__.TestObjectDB", "vlcp.service.manage.webapi.WebAPI"))
    

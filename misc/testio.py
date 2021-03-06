'''
Created on 2015/9/28

:author: hubo
'''

from __future__ import print_function, division
from vlcp.protocol import Protocol
from time import time
from vlcp.event import ConnectionWriteEvent, Event, withIndices, RoutineContainer, Client, TcpServer
import argparse
import sys
from vlcp.server import Server
import logging
from vlcp.event.core import TimerEvent
from vlcp.config.config import defaultconfig

@withIndices('state', 'connection')
class TestConnectionEvent(Event):
    UP = 'up'
    DOWN = 'down'

@defaultconfig
class TcpTestProtocol(Protocol):
    _default_buffersize = 1048576
    _default_totalsend = 10
    def __init__(self, server = False):
        self.server = server
    async def init(self, connection):
        await Protocol.init(self, connection)
        if not self.server:
            connection.subroutine(connection.executeWithTimeout(self.totalsend + 1.0, self._clientroutine(connection)), False, 'protocolroutine')
        else:
            await connection.write(ConnectionWriteEvent(connection, connection.connmark, data = b'', EOF = True))
        await connection.wait_for_send(TestConnectionEvent(TestConnectionEvent.UP, connection))
    async def closed(self, connection):
        await Protocol.closed(self, connection)
        await connection.wait_for_send(TestConnectionEvent(TestConnectionEvent.DOWN, connection))
    async def error(self, connection):
        await Protocol.error(self, connection)
        await connection.wait_for_send(TestConnectionEvent(TestConnectionEvent.DOWN, connection))
    async def _clientroutine(self, connection):
        # Send Data Until Connection closed
        try:
            data = b'\x00' * self.buffersize
            while True:
                we = ConnectionWriteEvent(connection, connection.connmark, data = data)
                await connection.write(we, False)
        except Exception:
            await connection.shutdown(True)
            raise
    def parse(self, connection, data, laststart):
        return ([], 0)
    
class Sampler(RoutineContainer):
    def __init__(self, scheduler = None, interval = 10.0, server = False):
        RoutineContainer.__init__(self, scheduler, True)
        self.connections = {}
        self.identifiers = {}
        self.idg = 0
        self.interval = interval
        self.server = server
    async def main(self):
        em = TestConnectionEvent.createMatcher()
        self.sampler = None
        while True:
            ev = await em
            if ev.state == TestConnectionEvent.UP:
                if self.server:
                    self.connections[ev.connection] = ev.connection.totalrecv
                else:
                    self.connections[ev.connection] = ev.connection.totalsend
                identifier = '[%3d]' % (ev.connection.socket.fileno(),)
                self.identifiers[ev.connection] = identifier
                print('%s Connected: %r' % (identifier, ev.connection))
                if not self.sampler:
                    self.subroutine(self._sampleroutine(), True, 'sampler', True)
            else:
                print('%s Disconnected' % (self.identifiers[ev.connection]))
                del self.connections[ev.connection]
                del self.identifiers[ev.connection]
                if not self.connections:
                    self.terminate(self.sampler)
                    self.sampler = None
    async def _sampleroutine(self):
        th = self.scheduler.setTimer(self.interval, self.interval)
        try:
            tm = TimerEvent.createMatcher(th)
            lt = time()
            t = 0
            interval = self.interval
            while True:
                await tm
                ct = time()
                tc = 0
                if self.connections:
                    for connection,lc in list(self.connections.items()):
                        if self.server:
                            cc = connection.totalrecv
                        else:
                            cc = connection.totalsend
                        speed = (cc - lc) / (ct - lt)
                        tc += (cc - lc)
                        unit = ''
                        if speed > 1024.0:
                            speed /= 1024.0
                            unit = 'K'
                            if speed > 1024.0:
                                speed /= 1024.0
                                unit = 'M'
                                if speed > 1024.0:
                                    speed /= 1024.0
                                    unit = 'G'
                        print('%s\t%.1fs - %.1fs\t%.2f%sB/s(%.2f%sbit/s)' % (self.identifiers[connection], t * interval, (t+1) * interval, speed, unit, speed * 8, unit))
                        self.connections[connection] = cc
                    if len(self.connections) > 1:
                        speed = tc / (ct - lt)
                        unit = ''
                        if speed > 1024.0:
                            speed /= 1024.0
                            unit = 'K'
                            if speed > 1024.0:
                                speed /= 1024.0
                                unit = 'M'
                                if speed > 1024.0:
                                    speed /= 1024.0
                                    unit = 'G'
                        print('[SUM]\t%.1fs - %.1fs\t%.2f%sB/s(%.2f%sbit/s)' % (t * interval, (t+1) * interval, speed, unit, speed * 8, unit))
                lt = ct
                t += 1
        finally:
            self.scheduler.cancelTimer(th)

if __name__ == '__main__':
    logging.basicConfig()
    parse = argparse.ArgumentParser(description='Test TCP IO Bandwidth')
    parse.add_argument('-s', '--server', action='store_true', help='Start in server mode')
    parse.add_argument('-c', '--client', help='Start in client mode and connect to IP', metavar='IP')
    parse.add_argument('-i', '--interval', type=float, help='Display bandwidth in interval', default = 10.0)
    parse.add_argument('-t', '--time', type=float, help='Run specified time', default = 10.0)
    parse.add_argument('-B', '--bind', help='When in server mode, bind to specified address', default='0.0.0.0')
    parse.add_argument('-p', '--port', type=int, help='When in server mode, bind to specified port (default 5987)', default=5987)
    parse.add_argument('-P', '--parallel', type=int, help='When in client mode, start multiple clients', metavar = 'NUM', default=1)
    args = parse.parse_args()
    s = Server()
    if args.server and args.client:
        print('Cannot specify both --server and --client')
        sys.exit(1)
    elif not args.server and not args.client:
        print('Must specify either --server or --client')
        sys.exit(1)
    elif args.server:
        tp = TcpTestProtocol(True)
        conn = TcpServer('tcp://%s:%d/' % (args.bind, args.port), tp, s.scheduler)
        sampler = Sampler(s.scheduler, args.interval, True)
        conn.start()
        sampler.start()
    else:
        tp = TcpTestProtocol(False)
        tp.totalsend = args.time
        sampler = Sampler(s.scheduler, args.interval, False)
        for _ in range(0, args.parallel):
            conn = Client('tcp://%s:%d/' % (args.client, args.port), tp, s.scheduler)
            conn.start()
        sampler.start()
    s.serve()

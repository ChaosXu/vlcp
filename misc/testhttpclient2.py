'''
Created on 2015/8/27

:author: hubo
'''
from __future__ import print_function
from vlcp.server import main
from vlcp.server.module import Module
from vlcp.event import RoutineContainer
from vlcp.utils.webclient import WebClient
from vlcp.config.config import manager
import re
from vlcp.event.core import SystemControlLowPriorityEvent
from vlcp.protocol.http import HttpProtocolException

urlmatcher = re.compile(br'https?\://[a-zA-Z0-9%\-\._~\[\]\:\@]+/[a-zA-Z0-9%\-\._~/;+=&]+(?:\?[a-zA-Z0-9%\-\._~/;+&=]+)?')

class MainRoutine(RoutineContainer):
    def __init__(self, scheduler=None, daemon=False):
        RoutineContainer.__init__(self, scheduler=scheduler, daemon=daemon)
    async def robot(self, wc, url, referer = None):
        if self.robotcount > 1000:
            return
        if url in self.urls:
            return
        headers = {}
        if referer:
            headers['Referer'] = referer
        self.urls.add(url)
        self.robotcount += 1
        try:
            resp = await wc.urlopen(self, url, headers = headers, autodecompress = True, timeout = 30, rawurl=True)
        except (IOError, HttpProtocolException) as exc:
            print('Failed to open %r: %s' % (url, exc))
            return
        try:
            if resp.get_header('Content-Type', 'text/html').lower().startswith('text/'):
                try:
                    timeout, data = await self.execute_with_timeout(60, resp.stream.read(self, 32768))
                except Exception as exc:
                    print('Error reading ', url, str(exc))
                if not timeout:
                    for match in urlmatcher.finditer(data):
                        newurl = match.group()
                        self.subroutine(self.robot(wc, newurl, url), False)
                    print('Finished: ', url)
                else:
                    print('Read Timeout: ', url)
            else:
                print('Not a text type: ', url)
            await resp.shutdown()
        finally:
            resp.close()
    async def main(self):
        self.urls = set()
        wc = WebClient(True)
        resp = await wc.urlopen(self, 'http://www.baidu.com/', autodecompress = True)
        print('Response received:')
        print(resp.fullstatus)
        print()
        print('Headers:')
        for k,v in resp.headers:
            print('%r: %r' % (k,v))
        print()
        print('Body:')
        if resp.stream is None:
            print('<Empty>')
        else:
            try: 
                while True:
                    data = await resp.stream.read(self, 1024)
                    #print(data, end = '')
            except EOFError:
                pass
            print(resp.connection.http_parsestage)
            print()
            resp = await wc.urlopen(self, 'http://www.baidu.com/favicon.ico', autodecompress = True)
            print('Response received:')
            print(resp.fullstatus)
            print()
            print('Headers:')
            for k,v in resp.headers:
                print('%r: %r' % (k,v))
            print()
            print('Body:')
            if resp.stream is None:
                print('<Empty>')
            else:
                data = await resp.stream.read(self)
                print('<Data: %d bytes>' % (len(data),))
        self.robotcount = 0
        self.subroutine(self.robot(wc, 'http://www.baidu.com/'))

class MainModule(Module):
    def __init__(self, server):
        Module.__init__(self, server)
        self.routines.append(MainRoutine(self.scheduler))
    
if __name__ == '__main__':
    #manager['server.debugging'] = True
    main()
    

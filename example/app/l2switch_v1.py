from optparse import OptionParser
import logging

from vlcp.event import RoutineContainer,TcpServer
from vlcp.server import Server
from vlcp.config.config import manager

from vlcp.protocol.openflow import Openflow,OpenflowConnectionStateEvent,OpenflowAsyncMessageEvent
from vlcp.protocol.openflow import common
from vlcp.utils.ethernet import ethernet_l2, mac_addr, mac_addr_bytes


# decide which openflow protocl used
of_proto = Openflow((common.OFP13_VERSION,))

# every app run as a routine, so inherit from RoutineContainer
class l2switch(RoutineContainer):
    def __init__(self,scheduler,daemon=None):
        super(l2switch,self).__init__(scheduler,daemon)
        
        # this app suport openflow version

        self.datapaths = set()
        self.mac_to_port = {}
    
    async def add_flow(self,parser,connection,cookie = 0,cookie_mask=0,table_id = 0,
            priority = 0,idle_time = 0,hard_time = 0,match = None,action = None,buffer_id=None):

        flowRequest = parser.ofp_flow_mod()
        flowRequest.table_id = table_id
        flowRequest.command = parser.OFPFC_ADD
        flowRequest.priority = priority
        flowRequest.buffer_id = parser.OFP_NO_BUFFER
        flowRequest.cookie = cookie
        flowRequest.cookie_mask = cookie_mask
        flowRequest.idle_timeout = idle_time
        flowRequest.hard_timeout = hard_time

        #flowRequest.out_port
        #flowRequest.out_group

        flowRequest.flags = parser.OFPFF_SEND_FLOW_REM
        flowRequest.match = match
        
        instruction = parser.ofp_instruction_actions(type=parser.OFPIT_APPLY_ACTIONS)
        instruction.actions.append(action)

        flowRequest.instructions.append(instruction)
        #log.debug('%r',flowRequest._tobytes()) 
        #log.debug('%r',common.dump(flowRequest))
        
        await of_proto.batch([flowRequest],connection,self)
       
    async def switch_add_handler(self,event):
        ofpParser = event.connection.openflowdef
        
        # save datapath 
        self.datapaths.add(event.datapathid)
        # init mac_to_port
        self.mac_to_port.setdefault(event.datapathid,{})

        # of 1.3 features have no port info
        # we must send the request
        portDescRequest = ofpParser.ofp_multipart_request(type=ofpParser.OFPMP_PORT_DESC)

        openflow_reply = await of_proto.querymultipart(portDescRequest,event.connection,self)
        
        for portpart in openflow_reply:
            for port in portpart.ports:
                log.debug('port no = %r',port.port_no)
                log.debug('port name = %r',port.name)
                log.debug('port mac = %r',port.hw_addr)

        #add default flow match everying
        match = ofpParser.ofp_match_oxm()
        action = ofpParser.ofp_action_output(port = ofpParser.OFPP_CONTROLLER,
                max_len = ofpParser.OFPCML_NO_BUFFER)

        await self.add_flow(connection=event.connection,parser=ofpParser,
                table_id = 0,priority = 0,match = match,action = action)
        # after perpare everying , while true handle event
        while True:  
            asyncEventMatcher = OpenflowAsyncMessageEvent.createMatcher()
            event = await asyncEventMatcher

            if event.type == ofpParser.OFPT_PORT_STATUS:
                self.subroutine(self.portStatsHandler(event))
        
            if event.type == ofpParser.OFPT_PACKET_IN:
                self.subroutine(self.packet_in_handler(event))
        

    async def switch_del_handler(self,event):
        self.datapaths.remove(event.datapathid)
        del self.mac_to_port[event.datapathid]
        
    def connectStateHandler(self,event):
       
    # connection event will carry features event here
        #featuresReply = common.dump(self.event.connection.openflow_featuresreply)
                
        # this is the negotiate version proto
        #ofpVersion = event.connection.openflowdef

        if event.state == 'setup':
            self.subroutine(self.switch_add_handler(event)) 
        
        #for m in self.switch_add_handler(self.event):
        #    yield m
        elif event.state == 'down':
            self.subroutine(self.switch_del_handler(event))

    async def portStatsHandler(self,event):
        pass
            
    async def packet_in_handler(self,event):

        datapathid = event.datapathid
        buffer_id = event.message.buffer_id 
        ofpParser = event.connection.openflowdef

        #log.debug('event message = %r',common.dump(event.message))
        for oxm in event.message.match.oxm_fields:
            if oxm.header == ofpParser.OXM_OF_IN_PORT:
                port_str = ''.join('%d' % n for n in bytearray(oxm.value))
                in_port = int(port_str)    
        
            if datapathid not in self.mac_to_port:
                return

            ethernet = ethernet_l2.create(event.message.data)
            dstMac = mac_addr.tobytes(ethernet.dl_dst)
            srcMac = mac_addr.tobytes(ethernet.dl_src)
            
            log.debug("packet in %r,%r, %r", mac_addr_bytes.formatter(srcMac), mac_addr_bytes.formatter(dstMac), in_port)
            self.mac_to_port[datapathid][srcMac] = in_port
            
            data = event.message.data

            if dstMac in self.mac_to_port[datapathid]:
                # packet out to mac_to_port[datapath][dstMac]
                # add an flow avoid next packet in
                
                output = self.mac_to_port[datapathid][dstMac]   
                await self.packetout(ofpParser,event.connection,in_port,output,buffer_id,data)
        
                match = ofpParser.ofp_match_oxm()
                match.oxm_fields.append(ofpParser.create_oxm(ofpParser.OXM_OF_ETH_DST,dstMac))
                action = ofpParser.ofp_action_output(port = output)
        
                await self.add_flow(connection=event.connection,parser=ofpParser,
                        table_id = 0,priority = 100,match = match,action = action)

            else:
                # flood this packet
                output = ofpParser.OFPP_FLOOD
                await self.packetout(ofpParser,event.connection,in_port,output,buffer_id,data)

    async def packetout(self,parser,connection,in_port,output,buffer_id,data):
        packetoutMessage = parser.ofp_packet_out()
        packetoutMessage.buffer_id = buffer_id
        packetoutMessage.in_port = in_port
        
        buffer_data = b''
        if buffer_id == parser.OFP_NO_BUFFER:
            buffer_data = data 
        
        packetoutMessage.data = buffer_data
        
        action = parser.ofp_action_output(port = output)
        
        packetoutMessage.actions.append(action)
    
        log.debug("packet to %r",output)
        await of_proto.batch([packetoutMessage],connection,self)

    async def main(self):
        while True:

            connectEventMatcher = OpenflowConnectionStateEvent.createMatcher()
            event = await connectEventMatcher
           
            # here event must be connect event
            self.connectStateHandler(event)

if __name__ == '__main__':

   
    #logging.basicConfig(format='%(asctime)s-%(name)s-%(levelname)s : %(message)s',level=logging.DEBUG)
    logging.basicConfig(format='%(asctime)s-%(name)s-%(levelname)s : %(message)s')
    log = logging.getLogger('l2switch')
    log.setLevel(logging.DEBUG)

    of_proto.debuging = True
    
    #manager['server.debugging']=True
    loopServer = Server()
    loopServer.scheduler.logger.setLevel(logging.DEBUG)

    tcpServer = TcpServer("tcp://127.0.0.1",of_proto,loopServer.scheduler)
    tcpServer.start()
    
    switch = l2switch(loopServer.scheduler)
    switch.start()

    loopServer.serve()

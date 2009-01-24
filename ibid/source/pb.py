import logging

from twisted.spread import pb
from twisted.application import internet
from twisted.internet import reactor

import ibid
from ibid.source import IbidSourceFactory
from ibid.event import Event

class IbidRoot(pb.Root):

    def __init__(self, name):
        self.name = name
        self.log = logging.getLogger('sources.%s' % name)

    def respond(self, event):
        return [response['reply'] for response in event.responses]

    def remote_message(self, message):
        event = Event(self.name, u'message')
        event.sender = event.sender_id = event.who = event.channel = self.name
        event.addressed = True
        event.public = False
        event.message = unicode(message, 'utf-8', 'replace')
        self.log.debug(u'message("%s")' % event.message)
        return ibid.dispatcher.dispatch(event).addCallback(self.respond)

    def remote_get_plugin(self, plugin, classname):
        self.log.debug(u'get_plugin("%s", "%s")' % (plugin, classname))
        __import__('ibid.plugins.%s' % plugin)
        klass = eval('ibid.plugins.%s.%s' % (plugin, classname))
        for processor in ibid.processors:
            if processor.name == plugin and isinstance(processor, klass) and issubclass(processor.__class__, pb.Referenceable):
                return processor
        return None

class SourceFactory(IbidSourceFactory):

    port = 8789

    def setServiceParent(self, service):
        root = pb.PBServerFactory(IbidRoot(self.name))
        if service:
            return internet.TCPServer(self.port, root).setServiceParent(service)
        else:
            reactor.listenTCP(self.port, root)

# vi: set et sta sw=4 ts=4:

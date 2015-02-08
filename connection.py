import socket
import logging
from threading import Thread
from queue import Queue
import queue

from models import Token, Device
from query import Query
import helpers
import config


class Connection(Thread):
    def __init__(self, parent, incomming_connection):
        Thread.__init__(self)
        self.running = True
        self.parent = parent
        self.data = b''
        self.connection, self.address = incomming_connection
        self.connection.settimeout(2.0)
        self.queries = Queue()
        self.other = None
        self.type = None
        self.logger = logging.getLogger('backdoor')
        self.logger.info('Connected with %s, %d' % (self.address[0], self.address[1]))

    def run(self):
        while self.running:
            try:
                self.update()
                data = self.connection.recv(1024)
                self.data += data

                if len(data) == 0:
                    self.logger.info('Connection to %s, %d lost. Shutting down connection thread.' % (self.address[0], self.address[1]))
                    if self.other in self.parent.webuis:
                        del self.parent.webuis[self.other]
                    elif self.other in self.parent.devices:
                        del self.parent.devices[self.other]
                    self.connection.close()
                    return

                data_stack = self.data.split(b'\r\n')
                self.data = data_stack.pop()
                for data in [x.decode('utf-8', errors='replace') for x in data_stack]:
                    self.update_queues(data)

            except socket.timeout as e:
                if e.args[0] == 'timed out':
                    continue
                else:
                    self.logger.exception('Shutting down connection thread (%s, %d) due to caught exception:' % (self.address[0], self.address[1]))
                    self.logger.exception(e)
                    self.running = False

            except socket.error as e:
                self.logger.exception('Shutting down connection thread (%s, %d) due to caught exception:' % (self.address[0], self.address[1]))
                self.logger.exception(e)
                self.running = False

            except Exception as e:
                if len(e.args[0]) == 0:
                    self.logger.exception('Shutting down connection thread (%s, %d) due to caught exception:' % (self.address[0], self.address[1]))
                    self.logger.exception(e)
                    self.running = False
        return

    def stop(self):
        self.logger.info('Regular shutdown of connection thread (%s, %d).' % (self.address[0], self.address[1]))
        self.connection.close()
        self.running = False

    def update(self):
        try:
            query = self.queries.get(block=False)
            self.connection.sendall(query.to_command())
        except queue.Empty:
            pass
        except Exception as e:
            self.logger.exception('Caught exception in thread (%s, %d) whilst trying to send query:' % (self.address[0], self.address[1]))
            self.logger.exception(query.query)
            self.logger.exception(e)

    @helpers.handle_dbsession()
    def update_queues(session, self, data):
        try:
            cmd = Query()
            if cmd.create_valid_query_from_string(data):
                try:
                    token = session.query(Device).filter_by(pubkey=cmd.token)
                    if cmd.method == 'REGISTER':
                        if token:
                            self.parent.devices[cmd.token] = self
                            self.other = cmd.token
                            self.type = 'device'
                            self.logger.info('Registered new device with token %s.' % cmd.token)
                        else:
                            self.logger.info('Unknown token %s tried to register and was rejected.' % cmd.token)

                    elif cmd.method == 'REGISTER WEBUI':
                        if cmd.token == config.webui_token:
                            self.parent.webuis[cmd.params[0]] = self
                            self.other = cmd.token
                            self.type = 'webui'
                            self.logger.info('Registered new webui with token %s.' % cmd.params[0])
                        else:
                            self.logger.info('Unknown token %s tried to register as webui and was rejected.' % cmd.token)

                    else:
                        self.parent.queries.put(cmd, block=False)
                except queue.Empty:
                    pass
                except Exception as e:
                    self.logger.exception('Caught exception whilst processing command:')
                    self.logger.exception(cmd.query)
                    self.logger.exception(e)
            else:
                self.logger.info('Discarded command:')
                self.logger.info(data)
        except:
            self.logger.info('Discarded command:')
            self.logger.info(data)
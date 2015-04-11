"""

    NFC controlled door access.
    Copyright (C) 2015  Yatekii yatekii(at)yatekii.ch

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU Affero General Public License as
    published by the Free Software Foundation, either version 3 of the
    License, or (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU Affero General Public License for more details.

    You should have received a copy of the GNU Affero General Public License
    along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""


import socket
import logging
from threading import Thread
from queue import Queue
import queue
import time

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
        self.last_ping = time.time()
        self.pinged = False

    def shutdown(self):
        self.logger.info('Shutting down connection thread (%s, %d):' % (self.address[0], self.address[1]))
        self.running = False

        if self.other in self.parent.webuis:
            del self.parent.webuis[self.other]
        elif self.other in self.parent.devices:
            del self.parent.devices[self.other]

        while True:
            try:
                self.connection.close()
                break
            except OSError as e:
                if e.args[0] == 9:
                    pass
                else:
                    raise e

        self.logger.info('Successfully shut down connection thread (%s, %d).' % (self.address[0], self.address[1]))

    def manage_ping(self):
        if time.time() - self.last_ping > config.ping_interval + config.pong_interval:
            self.logger.info('Got no PING from %s in time. Closing connection' % self.other)
            return self.shutdown()

        elif time.time() - self.last_ping > config.ping_interval and not self.pinged:
            query = Query()
            query.create_ping(config.server_token)
            self.connection.sendall(query.to_command())
            self.pinged = True
            self.logger.info('Sent PING to %s.' % self.other)

    def work_data(self):
        data = self.connection.recv(1024)
        self.data += data

        if len(data) == 0:
            return self.shutdown()

        data_stack = self.data.split(b'\n')
        self.data = data_stack.pop()
        for data in [x.decode('utf-8', errors='replace') for x in data_stack]:
            self.update_queues(data)

    def work_pending_queries(self):
        try:
            query = self.queries.get(block=False)
            self.connection.sendall(query.to_command())
        except queue.Empty:
            pass
        except Exception as e:
            self.logger.exception('Caught exception in thread (%s, %d) whilst trying to send query:' % (self.address[0], self.address[1]))
            self.logger.exception(query.query)
            self.logger.exception(e)

    def query_register(self, cmd, session):
        device_requesting = session.query(Device).filter_by(pubkey=cmd.token)
        if device_requesting:
            self.parent.devices[cmd.token] = self
            self.other = cmd.token
            self.type = 'device'
            self.logger.info('Registered new device with token %s.' % cmd.token)
        else:
            self.logger.info('Unknown token %s tried to register and was rejected.' % cmd.token)

    def query_register_webui(self, cmd):
        if cmd.token == config.webui_token:
            self.parent.webuis[cmd.params[0]] = self
            self.other = cmd.token
            self.type = 'webui'
            self.logger.info('Registered new webui with token %s.' % cmd.params[0])
        else:
            self.logger.info('Unknown token %s tried to register as webui and was rejected.' % cmd.token)

    def query_unregister(self, cmd):
        if cmd.token == self.other:
            self.logger.info('%s just unregistered. Shutting down.' % self.other)
            return self.shutdown()

    def query_pong(self, cmd):
        if self.pinged and config.ping_interval + config.pong_interval > self.last_ping - time.time():
            self.last_ping = time.time()
            self.pinged = False
            self.logger.info('Got PONG from %s.' % cmd.token)

    @helpers.handle_dbsession()
    def update_queues(session, self, data):
        try:
            cmd = Query()
            if cmd.create_valid_query_from_string(data):
                try:
                    if cmd.method == 'REGISTER':
                        self.query_register(cmd, session)

                    elif cmd.method == 'REGISTER WEBUI':
                        self.query_register_webui(cmd)

                    elif cmd.method == 'UNREGISTER':
                        self.query_unregister(cmd)

                    elif cmd.method == 'PONG':
                        self.query_pong(cmd)

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

    def run(self):
        while self.running:
            self.manage_ping()

            try:
                self.work_pending_queries()
                self.work_data()

            except socket.timeout as e:
                if e.args[0] == 'timed out':
                    continue
                else:
                    self.logger.exception('Caught exception in connection thread (%s, %d):'
                                          % (self.address[0], self.address[1]))
                    self.logger.exception(e)
                    return self.shutdown()

            except OSError as e:
                if e.args[0] == 9:
                    if self.running:
                        self.logger.info('Connection lost.')
                        return self.shutdown()
                else:
                    raise e

            except socket.error as e:
                self.logger.exception('Caught exception in connection thread (%s, %d):'
                                      % (self.address[0], self.address[1]))
                self.logger.exception(e)
                return self.shutdown()

            except Exception as e:
                if len(e.args[0]) == 0:
                    self.logger.exception('Caught exception in connection thread (%s, %d):'
                                          % (self.address[0], self.address[1]))
                    self.logger.exception(e)
                    return self.shutdown()
        return

    def stop(self):
        self.logger.info('Regular halt of connection thread (%s, %d).' % (self.address[0], self.address[1]))
        self.shutdown()
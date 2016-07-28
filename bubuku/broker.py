import json
import logging
import subprocess

from bubuku.config import KafkaProperties
from bubuku.id_generator import BrokerIdGenerator
from bubuku.zookeeper import Exhibitor

_LOG = logging.getLogger('bubuku.broker')


class BrokerManager(object):
    def __init__(self, kafka_dir: str, exhibitor: Exhibitor, id_manager: BrokerIdGenerator,
                 kafka_properties: KafkaProperties):
        self.kafka_dir = kafka_dir
        self.id_manager = id_manager
        self.exhibitor = exhibitor
        self.kafka_properties = kafka_properties
        self.process = None
        self.wait_timeout = 5 * 60

    def is_running_and_registered(self):
        if not self.process:
            return False
        return self.id_manager.is_registered()

    def stop_kafka_process(self):
        """
        Stops kafka process (if it is running) and says if this topic is still is a leader on in ISR list for some
        partitions
        :return: True, if broker is stopped and is not a leader or a isr.
        """
        self._terminate_process()
        self._wait_for_zk_absence()
        return not self._have_leadership()

    def _is_clean_election(self):
        value = self.kafka_properties.get_property('unclean.leader.election.enable')
        if value is None or value == 'true':
            return False
        return True

    def _have_leadership(self):
        # Only wait when unclean leader election is disabled
        if not self._is_clean_election():
            return False
        broker_id = str(self.id_manager.get_broker_id())
        if not broker_id:
            return False
        for topic in self.exhibitor.get_children('/brokers/topics'):
            for partition in self.exhibitor.get_children('/brokers/topics/{}/partitions'.format(topic)):
                state = json.loads(
                    self.exhibitor.get('/brokers/topics/{}/partitions/{}/state'.format(topic, partition))[0].decode(
                        'utf-8'))
                if str(state['leader']) == broker_id:
                    _LOG.warn('Broker {} is still a leader for {} {} ({})'.format(broker_id, topic, partition,
                                                                                  json.dumps(state)))
                    return True
                if any([str(x) == broker_id for x in state['isr']]):
                    _LOG.warn('Broker {} is still is in ISR for {} {} ({})'.format(broker_id, topic, partition,
                                                                                   json.dumps(state)))
                    return True
        return False

    def _terminate_process(self):
        if self.process is not None:
            try:
                self.process.terminate()
                self.process.wait()
            except Exception as e:
                _LOG.error('Failed to wait for termination of kafka process', exc_info=e)
            finally:
                self.process = None

    def _wait_for_zk_absence(self):
        try:
            self.id_manager.wait_for_broker_id_absence()
        except Exception as e:
            _LOG.error('Failed to wait until broker id absence in zk', exc_info=e)

    def get_zk_connect_string(self):
        return self.kafka_properties.get_property('zookeeper.connect')

    def _wait_for_clean_leader_election(self):
        if not self._is_clean_election():
            return True
        active_brokers = self.exhibitor.get_children('/brokers/ids')

        for topic in self.exhibitor.get_children('/brokers/topics'):
            for partition in self.exhibitor.get_children('/brokers/topics/{}/partitions'.format(topic)):
                state = json.loads(
                    self.exhibitor.get('/brokers/topics/{}/partitions/{}/state'.format(topic, partition))[0].decode(
                        'utf-8'))
                if str(state['leader']) not in active_brokers:
                    _LOG.warn('Leadership is not transferred for {} {} ({}, brokers: {})'.format(topic, partition,
                                                                                                 json.dumps(state),
                                                                                                 active_brokers))
                    return False
                if any([str(x) not in active_brokers for x in state['isr']]):
                    _LOG.warn('Leadership is not transferred for {} {} ({}, brokers: {})'.format(topic, partition,
                                                                                                 json.dumps(state),
                                                                                                 active_brokers))
                    return False
        return True

    def start_kafka_process(self, zookeeper_address):
        if self.process:
            return True
        broker_id = self.id_manager.get_broker_id()
        _LOG.info('Using broker_id {} for kafka'.format(broker_id))
        if broker_id is not None:
            self.kafka_properties.set_property('broker.id', broker_id)
        else:
            self.kafka_properties.delete_property('broker.id')

        _LOG.info('Using ZK address: {}'.format(zookeeper_address))
        self.kafka_properties.set_property('zookeeper.connect', zookeeper_address)

        self.kafka_properties.dump()

        _LOG.info('Staring kafka process')
        if not self._wait_for_clean_leader_election():
            return False
        self.process = self._open_process()

        _LOG.info('Waiting for kafka to start up with timeout {} seconds'.format(self.wait_timeout))
        if not self.id_manager.wait_for_broker_id_presence(self.wait_timeout):
            self.wait_timeout += 60
            _LOG.error(
                'Failed to wait for broker to start up, probably will kill, increasing timeout to {} seconds'.format(
                    self.wait_timeout))
        return True

    def _open_process(self):
        return subprocess.Popen(
            [self.kafka_dir + "/bin/kafka-server-start.sh", self.kafka_properties.settings_file])

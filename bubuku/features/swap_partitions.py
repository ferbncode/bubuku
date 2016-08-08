from collections import namedtuple
import logging
from operator import attrgetter

from bubuku.broker import BrokerManager
from bubuku.controller import Check
from bubuku.features.rebalance import BaseRebalanceChange
from bubuku.zookeeper import BukuExhibitor

_LOG = logging.getLogger('bubuku.features.swap_partitions')

TpData = namedtuple('TpData', ('topic', 'partition', 'size', 'replicas'))


class SwapPartitionsChange(BaseRebalanceChange):
    def __init__(self, zk: BukuExhibitor, fat_broker_id: str, slim_broker_id: str, gap: int, size_stats: dict):
        self.zk = zk
        self.fat_broker_id = fat_broker_id
        self.slim_broker_id = slim_broker_id
        self.gap = gap
        self.size_stats = size_stats
        self.to_move = None

    def run(self, current_actions):
        if self.should_be_cancelled(current_actions):
            _LOG.info("Cancelling swap partitions change as there are conflicting actions: {}".format(current_actions))
            return False

        # if there is already a swap which was postponed - just execute it
        if self.to_move:
            return not self.__perform_swap(self.to_move)

        # merge topics size stats to a single dict
        topics_stats = {}
        for broker_id, broker_stats in self.size_stats.items():
            for topic in broker_stats["topics"].keys():
                if topic not in topics_stats:
                    topics_stats[topic] = {}
                topics_stats[topic].update(broker_stats["topics"][topic])

        # find partitions that are candidates to be swapped between "fat" and "slim" brokers
        swap_partition_candidates = self.__find_all_swap_candidates(self.fat_broker_id, self.slim_broker_id,
                                                                    topics_stats)

        # smallest partition from slim broker is the one we move to fat broker
        slim_broker_smallest_partition = min(swap_partition_candidates[self.slim_broker_id], key=attrgetter("size"))
        if not slim_broker_smallest_partition:
            _LOG.info("No partitions on slim broker(id: {}) found to swap".format(self.slim_broker_id))
        _LOG.info("Slim broker(id: {}) partition to swap: {}".format(self.slim_broker_id, slim_broker_smallest_partition))

        # find the best fitting fat broker partition to move to slim broker
        # (should be as much as possible closing the gap between brokers)
        fat_broker_swap_candidates = swap_partition_candidates[self.fat_broker_id]
        matching_swap_partition = self.__find_best_swap_candidate(fat_broker_swap_candidates, self.gap,
                                                                  slim_broker_smallest_partition.size)

        # if there is no possible swap that will decrease the gap - just do nothing
        if not matching_swap_partition:
            _LOG.info("No candidate from fat broker(id:{}) found to swap".format(self.fat_broker_id))
            return False

        # write rebalance-json to ZK; Kafka will read it and perform the partitions swap
        self.to_move = self.__create_swap_partitions_json(slim_broker_smallest_partition, self.slim_broker_id,
                                                          matching_swap_partition, self.fat_broker_id)
        scheduled_rebalance = self.__perform_swap(self.to_move)
        if not scheduled_rebalance:
            _LOG.info("Swap partitions was postponed as there was a rebalance node in ZK")
        else:
            _LOG.info("Swap partitions rebalance was successfully scheduled in ZK")
        return not scheduled_rebalance

    def __perform_swap(self, swap_json):
        _LOG.info("Trying to swap partitions: {}".format(swap_json))
        return self.zk.reallocate_partitions(swap_json)

    def __find_all_swap_candidates(self, fat_broker_id: str, slim_broker_id: str, topics_stats: dict) -> dict:
        partition_assignment = self.zk.load_partition_assignment()
        swap_partition_candidates = {}
        for topic, partition, replicas in partition_assignment:
            if topic not in topics_stats or partition not in topics_stats[topic]:
                continue  # we skip this partition as there is not data size stats for it

            if fat_broker_id in replicas and slim_broker_id in replicas:
                continue  # we skip this partition as it exists on both involved brokers

            for broker_id in [slim_broker_id, fat_broker_id]:
                if broker_id in replicas:
                    if broker_id not in swap_partition_candidates:
                        swap_partition_candidates[broker_id] = []
                    swap_partition_candidates[broker_id].append(
                        TpData(topic, partition, topics_stats[topic][partition], replicas))
        return swap_partition_candidates

    @staticmethod
    def __find_best_swap_candidate(candidates: list, brokers_gap: int, partition_to_swap_size: int) -> TpData:
        candidates.sort(key=attrgetter("size"), reverse=True)
        matching_swap_partition = None
        smallest_new_gap = brokers_gap
        for tp in candidates:
            new_gap = abs(brokers_gap - 2 * abs(tp.size - partition_to_swap_size))
            if new_gap < smallest_new_gap:
                smallest_new_gap = new_gap
                matching_swap_partition = tp
        return matching_swap_partition

    @staticmethod
    def __create_swap_partitions_json(tp1: TpData, broker1: str, tp2: TpData, broker2: str) -> list:
        return [
            (tp1.topic, tp1.partition, [broker2 if r == broker1 else r for r in tp1.replicas]),
            (tp2.topic, tp2.partition, [broker1 if r == broker2 else r for r in tp2.replicas])
        ]


class CheckBrokersDiskImbalance(Check):
    def __init__(self, zk: BukuExhibitor, broker: BrokerManager, diff_threshold_kb: int):
        super().__init__(check_interval_s=900)
        self.zk = zk
        self.broker = broker
        self.diff_threshold_kb = diff_threshold_kb

    def check(self):
        if self.broker.is_running_and_registered():
            _LOG.info("Starting broker disk imbalance check")
            return self.create_swap_partition_change()
        return None

    def create_swap_partition_change(self) -> SwapPartitionsChange:
        size_stats = self.zk.get_disk_stats()
        if not size_stats or len(size_stats.keys()) == 0:
            _LOG.info("No size stats available, imbalance check cancelled")
            return None

        # find the most "slim" and the most "fat" brokers
        def free_size_getter(tup):
            return tup[1]["disk"]["free_kb"]

        slim_broker_id = max((item for item in size_stats.items()), key=free_size_getter)[0]
        fat_broker_id = min((item for item in size_stats.items()), key=free_size_getter)[0]
        fat_broker_free_kb = size_stats[fat_broker_id]["disk"]["free_kb"]
        slim_broker_free_kb = size_stats[slim_broker_id]["disk"]["free_kb"]

        # is the gap is big enough to swap partitions?
        gap = slim_broker_free_kb - fat_broker_free_kb
        if gap < self.diff_threshold_kb:
            _LOG.info("Gap between brokers: (id: {}, free_kb: {}) and (id: {}, free_kb: {}) is not enough to "
                      "trigger partitions swap; gap is {} KB".format(slim_broker_id, slim_broker_free_kb, fat_broker_id,
                                                                     fat_broker_free_kb, gap))
            return None
        else:
            _LOG.info("Creating swap partitions change to swap partitions of brokers: (id: {}, free_kb: {}) and "
                      "(id: {}, free_kb: {}); gap is {} KB".format(slim_broker_id, slim_broker_free_kb, fat_broker_id,
                                                                   fat_broker_free_kb, gap))
            return SwapPartitionsChange(self.zk, fat_broker_id, slim_broker_id, gap, size_stats)

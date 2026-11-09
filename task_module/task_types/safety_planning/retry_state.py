"""Region-local occupancy exhaustion, independent of ordinary/model failures."""

class OccupancyRetries:
    target_limit = 3

    def __init__(self):
        self.blocked_targets = {}
        self.excluded = {}

    def exclusions(self, region):
        return self.excluded.setdefault(tuple(region), set())

    def reject(self, *, region, cluster_id, target_id):
        region = tuple(region)
        tried = self.blocked_targets.setdefault((region, cluster_id), set())
        tried.add(target_id)
        if len(tried) >= self.target_limit:
            self.exclusions(region).add(cluster_id)
            return "next_time_cluster"
        return "next_gt"

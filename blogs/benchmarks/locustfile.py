import time
import random
from locust import task, User, between, constant_throughput
from crate import client

# If a host is provided through the Locust UI, that host will be used.
# Otherwise, there is a fallback to the host provided here.
CRATEDB_HOST = "http://blush-nien-nunb.aks1.westeurope.azure.cratedb.net:4200"

# Credentials are always used from here to not have them leak into the UI as
# part of the connection URL.
CRATEDB_USERNAME = "locust"
CRATEDB_PASSWORD = "load_test"


# CrateDBClient wraps the CrateDB client and returns results in a
# Locust-compatible data structure with additional metadata
class CrateDBClient:
    def __init__(self, host, request_event):
        self._connection = client.connect(
            servers=host or CRATEDB_HOST,
            username=CRATEDB_USERNAME,
            password=CRATEDB_PASSWORD,
        )
        self._request_event = request_event

    def send_query(self, *args, **kwargs):
        cursor = self._connection.cursor()
        start_time = time.perf_counter()

        request_meta = {
            "request_type": "CrateDB",
            "name": args[1],  # Static request name
            "response_length": 0,
            "response": None,
            "context": {},
            "exception": None,
        }

        response = None
        try:
            cursor.execute(args[0])
            response = cursor.fetchall()
        except Exception as e:
            request_meta["exception"] = e

        request_meta["response_time"] = (time.perf_counter() - start_time) * 1000
        request_meta["response"] = response
        request_meta["response_length"] = len(str(response))

        # Log the request in Locust
        self._request_event.fire(**request_meta)

        return response


class CrateDBUser(User):
    abstract = True

    def __init__(self, environment):
        super().__init__(environment)
        self.client = CrateDBClient(self.host, request_event=environment.events.request)


class QuickstartUser(CrateDBUser):
    wait_time = constant_throughput(1.0)

    # Precise Datacenter List
    datacenters = [
        "us-west-2a", "sa-east-1b", "eu-central-1b", "us-east-1b", "us-west-2c",
        "eu-west-1b", "us-west-1b", "ap-southeast-2a", "ap-southeast-1b", "eu-central-1a",
        "sa-east-1c", "sa-east-1a", "ap-southeast-2b", "ap-southeast-1a", "eu-west-1c",
        "eu-west-1a", "ap-northeast-1a", "us-west-1a", "us-west-2b", "ap-northeast-1c",
        "us-east-1a", "us-east-1c", "us-east-1e"
    ]

    # Service Environments
    service_environments = ["test", "production", "staging"]

    # Hosts for specific queries (host_1000 to host_3000)
    hosts = [f"host_{i}" for i in range(1000, 3001)]

    # -------------------------------
    # Point Queries (Fast Execution)
    # -------------------------------

    @task(10)
    def point_query_latest_cpu_metrics(self):
        random_host = random.choice(self.hosts)
        self.client.send_query(
            f"""
            SELECT
              usage_user,
              usage_system,
              usage_idle,
              usage_nice,
              usage_iowait,
              usage_irq,
              usage_softirq,
              usage_steal,
              usage_guest,
              usage_guest_nice
            FROM doc.cpu
            WHERE tags['hostname'] = '{random_host}'
              AND ts = (
                  SELECT max(ts) FROM doc.cpu WHERE tags['hostname'] = '{random_host}'
              );
            """,
            "Point Query - Latest CPU Metrics"
        )

    @task(8)
    def point_query_recent_cpu_snapshot(self):
        random_host = random.choice(self.hosts)
        self.client.send_query(
            f"""
            SELECT usage_user, usage_system, usage_idle
            FROM doc.cpu
            WHERE tags['hostname'] = '{random_host}'
              AND ts >= CURRENT_TIMESTAMP - INTERVAL '5 minutes'
            ORDER BY ts DESC
            LIMIT 1;
            """,
            "Point Query - Recent CPU Snapshot"
        )

    @task(9)
    def point_query_specific_service(self):
        service_env = random.choice(self.service_environments)
        self.client.send_query(
            f"""
            SELECT tags['hostname'], usage_user, usage_system
            FROM doc.cpu
            WHERE tags['service_environment'] = '{service_env}'
            ORDER BY ts DESC
            LIMIT 1;
            """,
            "Point Query - Specific Service"
        )

    @task(7)
    def point_query_high_cpu_hosts(self):
        self.client.send_query(
            """
            SELECT tags['hostname'], usage_user
            FROM doc.cpu
            WHERE usage_user > 80
            ORDER BY ts DESC
            LIMIT 5;
            """,
            "Point Query - High CPU Hosts"
        )

    # -------------------------------
    # Reduced Heavy Queries
    # -------------------------------

    @task(2)
    def query01_max_user_per_datacenter(self):
        datacenter_filter = random.choice(self.datacenters)
        self.client.send_query(
            f"""
            SELECT tags['datacenter'],
                   MAX_BY(tags['hostname'], usage_user) AS max_user_host,
                   MAX(usage_user) AS max_usage_user
            FROM doc.cpu
            WHERE tags['datacenter'] = '{datacenter_filter}'
            GROUP BY tags['datacenter'];
            """,
            "Max User CPU per Datacenter"
        )

    @task(1)
    def query02_moving_avg_user_cpu(self):
        datacenter_filter = random.choice(self.datacenters)
        self.client.send_query(
            f"""
            WITH aggregated AS (
              SELECT tags['hostname'] AS hostname,
                     DATE_TRUNC('minute', ts) AS minute_ts,
                     AVG(usage_user) AS avg_user_per_minute
              FROM doc.cpu
              WHERE tags['datacenter'] = '{datacenter_filter}'
              GROUP BY tags['hostname'], minute_ts
            )
            SELECT hostname,
                   minute_ts,
                   avg_user_per_minute,
                   AVG(avg_user_per_minute) OVER (PARTITION BY hostname ORDER BY minute_ts ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) AS moving_avg_user
            FROM aggregated;
            """,
            "Moving Avg User CPU (Datacenter Filter)"
        )


import datetime
import logging

from django.core.management.base import BaseCommand
from django.db.utils import OperationalError

from treeherder.model.models import Job, JobGroup, JobType, Machine, Repository
from treeherder.perf.exceptions import MaxRuntimeExceeded
from treeherder.perf.models import PerformanceDatum
from django.conf import settings

logging.basicConfig(format='%(levelname)s:%(message)s')

TREEHERDER = 'treeherder'
PERFHERDER = 'perfherder'
TREEHERDER_SUBCOMMAND = 'from:treeherder'
PERFHERDER_SUBCOMMAND = 'from:perfherder'
MINIMUM_PERFHERDER_EXPIRE_INTERVAL = 365

logger = logging.getLogger(__name__)


class DataCycler:
    source = ''

    def __init__(self, days, chunk_size, sleep_time, is_debug=None, logger=None, **kwargs):
        self.cycle_interval = datetime.timedelta(days=days)
        self.chunk_size = chunk_size
        self.sleep_time = sleep_time
        self.is_debug = is_debug or False
        self.logger = logger

    def cycle(self):
        pass


class TreeherderCycler(DataCycler):
    source = TREEHERDER.title()

    def cycle(self):
        self.logger.warning("Cycling jobs across all repositories")

        try:
            rs_deleted = Job.objects.cycle_data(
                self.cycle_interval, self.chunk_size, self.sleep_time
            )
            self.logger.warning("Deleted {} jobs".format(rs_deleted))
        except OperationalError as e:
            self.logger.error("Error running cycle_data: {}".format(e))

        self.remove_leftovers()

    def remove_leftovers(self):
        self.logger.warning('Pruning ancillary data: job types, groups and machines')

        def prune(id_name, model):
            self.logger.warning('Pruning {}s'.format(model.__name__))
            used_ids = Job.objects.only(id_name).values_list(id_name, flat=True).distinct()
            unused_ids = model.objects.exclude(id__in=used_ids).values_list('id', flat=True)

            self.logger.warning(
                'Removing {} records from {}'.format(len(unused_ids), model.__name__)
            )

            while len(unused_ids):
                delete_ids = unused_ids[: self.chunk_size]
                self.logger.warning('deleting {} of {}'.format(len(delete_ids), len(unused_ids)))
                model.objects.filter(id__in=delete_ids).delete()
                unused_ids = unused_ids[self.chunk_size :]

        prune('job_type_id', JobType)
        prune('job_group_id', JobGroup)
        prune('machine_id', Machine)


class PerfherderCycler(DataCycler):
    source = PERFHERDER.title()
    max_runtime = datetime.timedelta(hours=23)

    def __init__(self, days, chunk_size, sleep_time, is_debug=None, logger=None, **kwargs):
        super().__init__(days, chunk_size, sleep_time, is_debug, logger)
        if (
            days < MINIMUM_PERFHERDER_EXPIRE_INTERVAL
            and settings.SITE_HOSTNAME != 'treeherder-prototype2.herokuapp.com'
        ):
            raise ValueError(
                'Cannot remove performance data that is more recent than {} days'.format(
                    MINIMUM_PERFHERDER_EXPIRE_INTERVAL
                )
            )

    def cycle(self):
        started_at = datetime.datetime.now()

        removal_strategies = [
            MainRemovalStrategy(self.cycle_interval, self.chunk_size),
            TryDataRemoval(self.chunk_size),
        ]

        try:
            PerformanceDatum.objects.cycle_data(
                removal_strategies, self.logger, started_at, self.max_runtime
            )
        except MaxRuntimeExceeded as ex:
            logger.warning(ex)


class MainRemovalStrategy:
    """
    Removes `performance_datum` rows
    that are at least 1 year old.
    """

    def __init__(self, cycle_interval, chunk_size):
        self._cycle_interval = cycle_interval
        self._chunk_size = chunk_size
        self._max_timestamp = datetime.datetime.now() - cycle_interval
        self._manager = PerformanceDatum.objects

    def remove(self, using):
        """
        @type using: database connection cursor
        """
        chunk_size = self._find_ideal_chunk_size()
        using.execute(
            '''
            DELETE FROM `performance_datum`
            WHERE push_timestamp < %s
            LIMIT %s
        ''',
            [self._max_timestamp, chunk_size],
        )

    def _find_ideal_chunk_size(self) -> int:
        max_id = self._manager.filter(push_timestamp__gt=self._max_timestamp).order_by('-id')[0].id
        older_ids = self._manager.filter(
            push_timestamp__lte=self._max_timestamp, id__lte=max_id
        ).order_by('id')[: self._chunk_size]

        return len(older_ids) or self._chunk_size


class TryDataRemoval:
    """
    Removes `performance_datum` rows
    that originate from `try` repository and
    that are more than 6 weeks old.
    """

    def __init__(self, chunk_size):
        self._cycle_interval = datetime.timedelta(weeks=4)
        self._chunk_size = chunk_size
        self._max_timestamp = datetime.datetime.now() - self._cycle_interval
        self._manager = PerformanceDatum.objects

        self.__try_repo_id = None

    @property
    def try_repo(self):
        if self.__try_repo_id is not None:
            return self.__try_repo_id

        self.__try_repo_id = Repository.objects.get(name='try').id
        return self.__try_repo_id

    def remove(self, using):
        """
        @type using: database connection cursor
        """
        chunk_size = self._find_ideal_chunk_size()
        using.execute(
            '''
            DELETE FROM `performance_datum`
            WHERE repository_id = %s AND push_timestamp < %s
            LIMIT %s
        ''',
            [self.try_repo, self._max_timestamp, chunk_size],
        )

    def _find_ideal_chunk_size(self) -> int:
        max_id = (
            self._manager.filter(
                push_timestamp__gt=self._max_timestamp, repository_id=self.try_repo
            )
            .order_by('-id')[0]
            .id
        )
        older_ids = self._manager.filter(
            push_timestamp__lte=self._max_timestamp, id__lte=max_id, repository_id=self.try_repo
        ).order_by('id')[: self._chunk_size]

        return len(older_ids) or self._chunk_size


class Command(BaseCommand):
    help = """Cycle data that exceeds the time constraint limit"""
    CYCLER_CLASSES = {
        TREEHERDER: TreeherderCycler,
        PERFHERDER: PerfherderCycler,
    }

    def add_arguments(self, parser):
        parser.add_argument(
            '--debug',
            action='store_true',
            dest='is_debug',
            default=False,
            help='Write debug messages to stdout',
        )
        parser.add_argument(
            '--days',
            action='store',
            dest='days',
            default=120,
            type=int,
            help='Data cycle interval expressed in days. '
            'Minimum {} days when expiring performance data.'.format(
                MINIMUM_PERFHERDER_EXPIRE_INTERVAL
            ),
        )
        parser.add_argument(
            '--chunk-size',
            action='store',
            dest='chunk_size',
            default=100,
            type=int,
            help=(
                'Define the size of the chunks ' 'Split the job deletes into chunks of this size'
            ),
        )
        parser.add_argument(
            '--sleep-time',
            action='store',
            dest='sleep_time',
            default=0,
            type=int,
            help='How many seconds to pause between each query. Ignored when cycling performance data.',
        )
        subparsers = parser.add_subparsers(
            description='Data producers from which to expire data', dest='data_source'
        )
        subparsers.add_parser(TREEHERDER_SUBCOMMAND)  # default subcommand even if not provided

        # Perfherder will have its own specifics
        subparsers.add_parser(PERFHERDER_SUBCOMMAND)

    def handle(self, *args, **options):
        logger.warning("Cycle interval... {} days".format(options['days']))

        data_cycler = self.fabricate_data_cycler(options, logger)
        logger.warning('Cycling {0} data...'.format(data_cycler.source))
        data_cycler.cycle()

    def fabricate_data_cycler(self, options, logger):
        data_source = options.pop('data_source') or TREEHERDER_SUBCOMMAND
        data_source = data_source.split(':')[1]

        cls = self.CYCLER_CLASSES[data_source]
        return cls(logger=logger, **options)

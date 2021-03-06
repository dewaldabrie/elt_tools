"""Generic Client for interacting with data sources."""
import datetime
import logging
from sqlalchemy import MetaData, Table
from typing import Dict, Set, List
from elt_tools.engines import engine_from_settings
from elt_tools.settings import ELT_PAIRS, DATABASES


def _construct_where_clause_from_timerange(
        start_datetime: datetime.datetime = None,
        end_datetime: datetime.datetime = None,
        timestamp_fields: List[str] = None,
        stick_to_dates: bool = False
):
    where_clause = ""

    if stick_to_dates and start_datetime == end_datetime:
        msg = "The date range for dates is inclusive of the start date and exclusive of the end date." \
              "Since your start and end date range is the same, your query will be meaningless."
        raise ValueError(msg)

    if (not timestamp_fields and start_datetime) or (not timestamp_fields and end_datetime):
        msg = "You've passed in a time range, but no timestamp field names."
        print(msg)
        raise ValueError(msg)

    if stick_to_dates:
        if start_datetime:
            start_datetime = start_datetime.date()
        if end_datetime:
            end_datetime = end_datetime.date()

    if timestamp_fields and start_datetime and end_datetime:
        where_clause += " WHERE " + " OR ".join([
            f"({timestamp_field} >= '{start_datetime}' AND {timestamp_field} < '{end_datetime}')"
            for timestamp_field in timestamp_fields
        ])
        return where_clause

    if timestamp_fields and start_datetime:
        where_clause += " WHERE " + " AND ".join([
            f"{timestamp_field} >= '{start_datetime}'"
            for timestamp_field in timestamp_fields
        ])
    if timestamp_fields and end_datetime:
        where_clause += " AND " + " AND ".join([
            f"{timestamp_field} < '{end_datetime}'"
            for timestamp_field in timestamp_fields
        ])
    return where_clause


class DataClient:
    # Override this if you want
    # to pass in your settings.
    databases = DATABASES

    def __init__(self, engine):
        self.engine = engine
        self.metadata = MetaData(bind=self.engine)
        self.table_name = None

    @classmethod
    def from_settings(cls, db_key, databases: Dict = None):
        if databases:
            db_settings = databases
        else:
            db_settings = cls.databases
        return cls(engine_from_settings(db_key, databases=db_settings))

    @property
    def table(self):
        if self.table_name:
            return Table(self.table_name, self.metadata, autoload=True)
        return None

    def insert_rows(self, rows, table=None, replace=None):
        """Insert rows into table."""
        if replace:
            self.engine.execute(f'TRUNCATE TABLE {table}')
        self.table_name = table
        self.engine.execute(self.table.insert(), rows)
        return self.construct_response(rows, table)

    def fetch_rows(self, query):
        """Fetch all rows via query."""
        rows = self.engine.execute(query).fetchall()
        return rows

    def query(self, query):
        return [dict(r) for r in self.fetch_rows(query)]

    @staticmethod
    def construct_response(rows, table):
        """Summarize results of an executed query."""
        columns = rows[0].keys()
        column_names = ", ".join(columns)
        num_rows = len(rows)
        return f'Inserted {num_rows} rows into `{table}` with {len(columns)} columns: {column_names}'

    def count(
            self,
            table_name,
            field_name=None,
            start_datetime: datetime.datetime = None,
            end_datetime: datetime.datetime = None,
            timestamp_fields: List[str] = None,
            stick_to_dates: bool = False
    ) -> int:
        """
        Optionally pass in timestamp fields and time range to limit the query_range.
        """
        if not field_name:
            field_name = "*"
        count_query = f"""
        SELECT COUNT({field_name}) AS count FROM {table_name}
        """
        where_clause = _construct_where_clause_from_timerange(
            start_datetime=start_datetime,
            end_datetime=end_datetime,
            timestamp_fields=timestamp_fields,
            stick_to_dates=stick_to_dates,
        )
        count_query += where_clause
        logging.debug("Count query is %s" % count_query)
        result = self.query(count_query)[0]['count']
        return result

    def find_duplicate_keys(self, table_name, key_field):
        """Find if a table has duplicates by a certain column, if so return all the instances that
        have duplicates together with their counts."""
        query = f"""
        SELECT {key_field}, COUNT({key_field}) as count
        FROM {table_name}
        GROUP BY 1
        HAVING count > 1;
        """
        return self.fetch_rows(query)


class ELTDBPair:
    # Override these if you want
    # to pass in your settings.
    elt_pairs = ELT_PAIRS
    databases = DATABASES

    def __init__(self, name: str, source: DataClient, target: DataClient):
        self.name = name
        self.source = source
        self.target = target

    @classmethod
    def from_settings(cls, name=None, db_key=None):
        if not name:
            name = db_key
        if not db_key:
            db_key = name
        source_target_settings = cls.elt_pairs[db_key]
        source_client = DataClient.from_settings(source_target_settings['source'], databases=cls.databases)
        target_client = DataClient.from_settings(source_target_settings['target'], databases=cls.databases)
        return cls(name, source_client, target_client)

    def __repr__(self):
        return self.name


    def compare_counts(
            self,
            table_name,
            field_name=None,
            start_datetime: datetime.datetime = None,
            end_datetime: datetime.datetime = None,
            timestamp_fields: List[str] = None,
            stick_to_dates: bool = False
    ) -> int:
        return self.target.count(
            table_name,
            field_name=field_name,
            start_datetime=start_datetime,
            end_datetime=end_datetime,
            timestamp_fields=timestamp_fields,
            stick_to_dates=stick_to_dates,
        ) - self.source.count(
            table_name,
            field_name=field_name,
            start_datetime=start_datetime,
            end_datetime=end_datetime,
            timestamp_fields=timestamp_fields,
            stick_to_dates=stick_to_dates,
        )

    def find_orphans(
            self,
            table_name,
            key_field,
            start_datetime: datetime.datetime = None,
            end_datetime: datetime.datetime = None,
            timestamp_fields: List[str] = None,
            stick_to_dates: bool = False
    ) -> Set:
        """
        Find orphaned records in BQ for which their source parents were deleted.
        Optionally pass in timestamp fields and time range to limit the amount of records
        to compare for orphans (for use on large tables).
        """
        all_ids_query = f"""
            SELECT {key_field} AS id FROM {table_name}
        """

        where_clause = _construct_where_clause_from_timerange(
            start_datetime=start_datetime,
            end_datetime=end_datetime,
            timestamp_fields=timestamp_fields,
            stick_to_dates=stick_to_dates,
        )
        all_ids_query += where_clause
        logging.debug("Id lookup for orphan query: %s" % all_ids_query)

        # First compare counts on limited date range to skip id comparison if no difference
        if where_clause:
            count_diff = self.compare_counts(
                table_name,
                start_datetime=start_datetime,
                end_datetime=end_datetime,
                timestamp_fields=timestamp_fields,
                stick_to_dates=stick_to_dates,
            )
            if count_diff == 0:
                return set()

        def format_id(row):
            id = row['id']
            if isinstance(id, int):
                return id
            else:
                return str(id)  # cast to str if UUID

        rows = self.target.fetch_rows(all_ids_query)
        target_ids = set(map(format_id, rows))

        rows = self.source.fetch_rows(all_ids_query)
        source_ids = set(map(format_id, rows))

        orphans = target_ids - source_ids
        return orphans

    def remove_orphans_from_target(
            self,
            table_name,
            key_field,
            start_datetime: datetime.datetime = None,
            end_datetime: datetime.datetime = None,
            timestamp_fields: List[str] = None,
            stick_to_dates: bool = False
    ) -> int:
        orphans = self.find_orphans(
            table_name,
            key_field,
            start_datetime=start_datetime,
            end_datetime=end_datetime,
            timestamp_fields=timestamp_fields,
            stick_to_dates=stick_to_dates
        )
        num_orphans = len(orphans)
        def format_primary_key(k):
            if isinstance(k, int):
                return str(k)
            else:
                return "'%s'" % str(k)
        orphan_ids_csv = ','.join(map(format_primary_key, orphans))

        if not orphan_ids_csv:
            logging.info("No orphans found for table %s" % table_name)
        else:
            logging.info("Found %d orphaned records in target %s." % (num_orphans, table_name))
            delete_query = f"""
            DELETE {table_name} 
            WHERE {key_field} IN ({orphan_ids_csv}) 
            """
            logging.info(delete_query)
            self.target.query(delete_query)

        return num_orphans

    def remove_orphans_from_target_with_binary_search(
            self,
            table_name,
            key_field,
            start_datetime: datetime.datetime = None,
            end_datetime: datetime.datetime = None,
            timestamp_fields: List[str] = None,
            stick_to_dates: bool = False,
            thres=10000,
            min_segment_size=datetime.timedelta(seconds=10),
    ):
        """
        Do binary search on table by recursively bifurcating the date range
        until the number of records in that range drops below the threshold.
        Once, below the threshold, find and remove the orphans for that segment.
        :return: Total number of records removed.
        """
        # If time range is not set, fetch it from the target database
        if not start_datetime:
            query = """
            SELECT {select_stmt}
            FROM {table_name}
            """.format(
                select_stmt=', '.join(map(lambda x: 'MIN({0}) AS {0} '.format(x), timestamp_fields)),
                table_name=table_name,
            )
            result = self.target.query(query)
            start_datetime = min(result[0].values())
        if not end_datetime:
            query = """
            SELECT {select_stmt}
            FROM {table_name}
            """.format(
                select_stmt=', '.join(map(lambda x: 'MAX({0}) AS {0} '.format(x), timestamp_fields)),
                table_name=table_name,
            )
            result = self.target.query(query)
            end_datetime = max(result[0].values())
            
        if stick_to_dates:
            if start_datetime:
                start_datetime = start_datetime.date()
            if end_datetime:
                end_datetime = end_datetime.date()

        def avg_datetime(start, end):
           return start + (end - start) / 2

        def bifurcate_time_range(start, end):
            halfway = avg_datetime(start, end)
            return start, halfway, end

        removed_count = 0
        count1 = count2 = 0
        start, halfway, end = bifurcate_time_range(start_datetime, end_datetime)
        logging.debug(f"Start date is : {start}")
        logging.debug(f"Halfway date is : {halfway}")
        logging.debug(f"End date is : {end}")

        if start != halfway:
            count1 = self.target.count(
                table_name,
                field_name=key_field,
                start_datetime=start,
                end_datetime=halfway,
                timestamp_fields=timestamp_fields,
                stick_to_dates=stick_to_dates,
            )
        if end != halfway:
            count2 = self.target.count(
                table_name,
                field_name=key_field,
                start_datetime=halfway,
                end_datetime=end,
                timestamp_fields=timestamp_fields,
                stick_to_dates=stick_to_dates,
            )

        logging.debug(f"Count of range 1 is {count1}")
        logging.debug(f"Count of range 2 is {count2}")

        # exit conditions
        if start == halfway or end == halfway or (halfway - start) < min_segment_size:
            return self.remove_orphans_from_target(
                table_name,
                key_field,
                start_datetime=start,
                end_datetime=end,
                timestamp_fields=timestamp_fields,
                stick_to_dates=stick_to_dates,
            )
        if count1 == 0 and count2 == 0:
            return 0
        if count1 < thres and count2 < thres:
            return self.remove_orphans_from_target(
                table_name,
                key_field,
                start_datetime=start,
                end_datetime=halfway,
                timestamp_fields=timestamp_fields,
                stick_to_dates=stick_to_dates,
            ) + self.remove_orphans_from_target(
                table_name,
                key_field,
                start_datetime=halfway,
                end_datetime=end,
                timestamp_fields=timestamp_fields,
                stick_to_dates=stick_to_dates,
            )

        # recursion conditions
        if count1 >= thres or count2 >= thres:
            removed_count += self.remove_orphans_from_target_with_binary_search(
                table_name,
                key_field,
                start_datetime=start,
                end_datetime=halfway,
                timestamp_fields=timestamp_fields,
                stick_to_dates=stick_to_dates,
                thres=thres,
            ) + self.remove_orphans_from_target_with_binary_search(
                table_name,
                key_field,
                start_datetime=halfway,
                end_datetime=end,
                timestamp_fields=timestamp_fields,
                stick_to_dates=stick_to_dates,
                thres=thres,
            )
        
        return removed_count


    def find_missing_in_target(self):
        """
        This needs to incorporate some form of time lag in the query on both source and target
        to compensate for ELT latency.
        """
        pass

    def list_common_tables(self):
        pass

    def fill_missing_target_records(self):
        pass

    def remove_duplicates_from_target(self):
        pass

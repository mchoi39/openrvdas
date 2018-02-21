#!/usr/bin/env python3

import logging
import sys

import mysql.connector as db

sys.path.append('.')

from logger.utils.formats import Python_Record
from logger.utils.das_record import DASRecord

from logger.writers.writer import Writer

# Based on https://dev.mysql.com/doc/connector-python/en/connector-python-example-connecting.html

################################################################################
class MySQLConnector(Writer):
  def __init__(self, database, host, user, password):
    """Interface to MySQLConnector, to be imported by, e.g. DatabaseWriter."""
    self.connection = db.connect(database=database, host=host,
                                 user=user, password=password)
    # Map from table_name->next id we're going to read from that table
    self.next_id = {}

    self.exec_sql_command('set autocommit = 1')

  ############################
  def exec_sql_command(self, command):
    cursor = self.connection.cursor()
    cursor.execute(command)
    self.connection.commit()
    cursor.close()
    
  ############################
  def table_name(self,  record):
    """Infer table name from record."""
    table_name = record.data_id
    if record.message_type:
      table_name += '#' + record.message_type

    # Clean up common, non-SQL-friendly characters
    #table_name = table_name.replace('$','').replace('-','_')
    return table_name

  ############################
  def table_exists(self, table_name):
    """Does the specified table exist in the database?"""
    cursor = self.connection.cursor()
    cursor.execute('SHOW TABLES LIKE "%s"' % table_name)
    if cursor.fetchone():
      exists = True
    else:
      exists = False
    cursor.close()
    return exists

  ############################
  TYPE_MAP = {
    int:   'int',
    float: 'double',
    str:   'text',
    bool:  'bool'
  }
  
  ############################
  def create_table_from_record(self,  record):
    """Create a new table with one column for each field in the record. Try
    to infer the proper type for each column based on the type of the value
    of the field."""

    table_name = self.table_name(record)
    if self.table_exists(table_name):
      logging.warning('Trying to create table that already exists: %s',
                      table_name)
      return

    # Id and timestamp are needed for all tables
    columns = ['`id` int(11) not null auto_increment',
               # '`message_type` text',
               '`timestamp` double not null']

    # Iterate through fields in record, figure out their type and
    # create an table type appropriate for each.
    for field in record.fields:
      value = record.fields[field]
      if not type(value) in self.TYPE_MAP:
        raise TypeError('Unrecognized value type in record: %s', type(value))
      columns.append('`%s` %s' %( field, self.TYPE_MAP[type(value)]))

    table_cmd = 'create table `%s` (%s, primary key (`id`))' % \
                (table_name, ','.join(columns))
    logging.info('Creating table with command: %s', table_cmd)
    self.exec_sql_command(table_cmd)

  ############################
  def write_record(self, record):
    """Write record to table."""

    # Helper function for formatting
    def map_value_to_str(value):
      if type(value) in [int, float]:
        return str(value)
      elif type(value) is str:
        return '"%s"' % value
      elif type(value) is bool:
        return '1' if value else '0'
      
    table_name = self.table_name(record)

    keys = record.fields.keys()
    write_cmd = 'insert into `%s` (`timestamp`,%s) values (%f,%s)' % \
                (table_name, ','.join(keys), record.timestamp,
                 ','.join([map_value_to_str(record.fields[k]) for k in keys]))
    
    logging.debug('Inserting record into table with command: %s', write_cmd)
    self.exec_sql_command(write_cmd)

  ############################
  def _parse_table_name(self, table_name):
    """Parse table name into data_id and message_type."""
    if '#' in table_name:
      (data_id, message_type) = table_name.split(sep='#', maxsplit=1)
    else:
      data_id = table_name
      message_type = None
    return (data_id, message_type)

  ############################
  def _get_table_columns(self, table_name):
    """Get columns (we could probably cache these, checking against the
    existence of self.last_record_read[table_name] to know when we
    need to rebuild."""
    cursor = self.connection.cursor()
    cursor.execute('show columns in `%s`' % table_name)
    columns = [c[0] for c in cursor]
    logging.debug('Columns: %s', columns)
    return columns

  ############################
  def _num_rows(self, table_name):
    query = 'select count(1) from `%s`' % table_name
    cursor = self.connection.cursor()
    cursor.execute(query)
    num_rows = next(cursor)[0]
    return num_rows
  
  ############################
  def _fetch_and_parse_records(self, table_name, query):
    """Fetch records, give DB query, and parse into DASRecords."""

    (data_id, message_type) = self._parse_table_name(table_name)
    columns = self._get_table_columns(table_name)

    cursor = self.connection.cursor()
    cursor.execute(query)

    results = []
    for values in cursor:
      logging.debug('value: %s', values)
      fields = dict(zip(columns, values))
      id = fields.pop('id')
      self.next_id[table_name] = id + 1
      
      timestamp = fields.pop('timestamp')      
      results.append(DASRecord(data_id=data_id, message_type=message_type,
                               timestamp=timestamp, fields=fields))
    cursor.close()
    return results

  ############################
  def read(self,  table_name, start=None):
    """Read the next record from table. If start is specified, reset read
    to start at that position."""

    if  start is None:
      if not table_name in self.next_id:
        self.next_id[table_name] = 1
      start = self.next_id[table_name]
      
    query = 'select * from `%s` where (id = %d)' % (table_name, start)
    result = self._fetch_and_parse_records(table_name, query)

    if not result:
      return None
    return result[0]

  ############################
  def seek(self,  table_name, offset=0, origin='current'):
    """Behavior is intended to mimic file seek() behavior but with
    respect to records: 'offset' means number of records, and origin
    is either 'start', 'current' or 'end'."""

    if not table_name in self.next_id:
      self.next_id[table_name] = 1
    
    if origin == 'current':
      self.next_id[table_name] += offset
    elif origin == 'start':
      self.next_id[table_name] = offset + 1
    elif origin == 'end':
      num_rows = self._num_rows(table_name)
      self.next_id[table_name] = num_rows + offset + 1
      
    logging.debug('Seek: next position table %s %d',
                  table_name, self.next_id[table_name])

  ############################
  def read_range(self,  table_name, start=None, stop=None):
    """Read one or more records from table. If start is not specified,
    begin reading at the next not-yet-read record. If stops is
    not specified, read as many records as are available."""

    if  start is None:
      if not table_name in self.next_id:
        self.next_id[table_name] = 1
      start = self.next_id[table_name]
      
    condition_list = ['id >= %d' % start]
    if stop is not None:
      condition_list.append('id < %d' % stop)
    condition_clause = 'where (%s)' % ' and '.join(condition_list)

    query = 'select * from `%s` %s' % (table_name, condition_clause)
    return self._fetch_and_parse_records(table_name, query)

  ############################
  def read_time_range(self,  table_name, start_time=None, stop_time=None):
    """Read one or more records from table. If start_time is not
    specified, begin reading at the earliest record. If stop_time is
    not specified, read to the most recent."""

    condition_list = []
    if  start_time is not None:
      condition_list.append('timestamp >= %f' % start_time)
    if  stop_time is not None:
      condition_list.append('timestamp < %f' % stop_time)

    if condition_list:
      condition_clause = 'where (%s)' % ' and '.join(condition_list)
    else:
      condition_clause = ''

    query = 'select * from `%s` %s' % (table_name, condition_clause)
    return self._fetch_and_parse_records(table_name, query)
    
  ############################
  def delete_table(self,  table_name):
    """Delete a table."""
    delete_cmd = 'drop table `%s`' % table_name
    logging.info('Dropping table with command: %s', delete_cmd)
    self.exec_sql_command(delete_cmd)

    # Clear out our recollection of how far into the table we've read
    if table_name in self.next_id:
      del self.next_id[table_name]

  ############################
  def close(self):
    """Close connection."""
    self.connection.close()
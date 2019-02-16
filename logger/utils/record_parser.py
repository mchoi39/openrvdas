#!/usr/bin/env python3

"""Tools for parsing NMEA and other text records.

TODO: text to describe device and device_type formats
"""

import datetime
import glob
import json
import logging
import parse
import pprint
import sys

# Append openrvdas root to syspath prior to importing openrvdas modules
from os.path import dirname, realpath; sys.path.append(dirname(dirname(dirname(realpath(__file__)))))

from logger.utils import read_config
from logger.utils.das_record import DASRecord

DEFAULT_DEFINITION_PATH = 'local/devices/*.yaml'
DEFAULT_RECORD_FORMAT = '{data_id:w} {timestamp:ti} {message}'

################################################################################
class RecordParser:
  ############################
  def __init__(self, record_format=DEFAULT_RECORD_FORMAT,
               definition_path=DEFAULT_DEFINITION_PATH,
               return_das_record=False, return_json=False):
    """Create a parser that will parse field values out of a text record
    and return either a Python dict of data_id, timestamp and fields,
    a JSON encoding of that dict, or a binary DASRecord.

    record_format - string for parse.parse() to use to break out data_id
        and timestamp from the rest of the message. By default this will
        look for 'data_id timestamp device_message'.

    definition_path - a comma-separated set of file globs in which to look
        for device and device_type definitions with which to parse message.

    return_json - return the parsed fields as a JSON encoded dict

    return_das_record - return the parsed fields as a DASRecord object

    """
    self.record_format = record_format
    self.compiled_record_format = parse.compile(record_format)
    self.return_das_record = return_das_record
    self.return_json = return_json
    if return_das_record and return_json:
      raise ValueError('Only one of return_json and return_das_record '
                       'may be true.')

    # Fill in the devices and device_types
    definitions = self._read_definitions(definition_path)
    self.devices = {name:definition
                    for name, definition in definitions.items()
                    if definition.get('category', None) == 'device'}
    self.device_types = {name:definition
                         for name, definition in definitions.items()
                         if definition.get('category', None) == 'device_type'}

    # Some limited error checking: make sure that all devices have a
    # defined device_type.
    for device, device_def in self.devices.items():
      device_type = device_def.get('device_type', None)
      if not device_type:
        raise ValueError('Device definition for "%s" has no declaration of '
                         'its device_type.' % device)
      if not device_type in self.device_types:
        raise ValueError('Device type "%s" (declared in definition of "%s") '
                         'is undefined.' % (device_type, device))
    
    # Compile format definitions so that we can run them more
    # quickly. If format is a single string, normalize it into a list
    # to simplify later code.
    for device_type, device_type_def in self.device_types.items():
      format = device_type_def.get('format', None)
      if format is None:
        raise ValueError('Device type %s has no format definition' %device_type)
      if type(format) is str:
        compiled_format = [parse.compile(format)]
      else:
        compiled_format = [parse.compile(f) for f in format]
      self.device_types[device_type]['compiled_format'] = compiled_format

  ############################
  def parse_record(self, record):
    """Parse an id-prefixed text record into a Python dict of data_id,
    timestamp and fields.
    """
    if not record:
      return None
    if not type(record) is str:
      logging.info('Record is not string: "%s"', record)
      return None
    try:
      parsed_record = self.compiled_record_format.parse(record).named
    except (ValueError, AttributeError):
      logging.warning('Unable to parse record into "%s"', self.record_format)
      logging.warning('Record: %s', record)
      return None
    
    # Convert timestamp to numeric, if it's there
    timestamp = parsed_record.get('timestamp', None)
    if timestamp is not None and type(timestamp) is datetime.datetime:
      timestamp = timestamp.timestamp()
      parsed_record['timestamp'] = timestamp

    # Figure out what kind of message we're expecting, based on data_id
    data_id = parsed_record.get('data_id', None)
    if data_id is None:
      logging.warning('No data id found in record: %s', record)
      return None

    # Get device and device_type definitions for data_id
    device = self.devices.get(data_id, None)
    if not device:
      logging.warning('Unrecognized data id "%s" in record: %s', data_id,record)
      logging.warning('Devices are: %s', ', '.join(self.devices.keys()))
      return None

    device_type = device.get('device_type', None)
    if not device_type:
      logging.error('Internal error: No "device_type" for device %s!', device)
      return None

    # Extract the message we're going to parse
    message = parsed_record.get('message', None)
    if message is None:
      logging.warning('No message found in record: %s', record)
      return None
    del parsed_record['message']

    # Now parse the message, based on device type. If something goes
    # wrong during parsing, expect a ValueError.      
    try:
      parsed_fields = self.parse(device_type=device_type, message=message)
      logging.debug('Got fields: %s', pprint.pformat(parsed_fields))
    except ValueError as e:
      logging.error(str(e))
      return None

    # Finally, convert field values to variable names specific to device
    device_fields = device.get('fields', None)
    if not device_fields:
      logging.error('No "fields" definition found for device %s', data_id)
      return None

    # Assign field values to the appropriate named variable.
    fields = {}
    for field_name, value in parsed_fields.items():
      # If it's a datetime, convert to numeric timestamp
      if type(value) is datetime.datetime:
        value = value.timestamp()

      variable_name = device_fields.get(field_name, None)
      if variable_name is None:
        logging.debug('Got unrecognized field "%s" for %s; ignoring',
                      field_name, device_type)
      else:
        fields[variable_name] = value

    parsed_record['fields'] = fields
    logging.debug('Returning parsed record: %s', pprint.pformat(parsed_record))
        
    if self.return_das_record:
      try:
        return DASRecord(data_id=data_id, timestamp=timestamp, fields=fields)
      except KeyError:
        return None

    elif self.return_json:
      return json.dumps(parsed_record)
    else:
      return parsed_record

  ############################
  def parse(self, device_type, message):
    """Parse a text message of device_type into a flat Python dict; raise
    ValueError if there are problems, return empty dict if there is no
    match to the provided formats.
    """
    device_definition = self.device_types.get(device_type, None)
    if not device_definition:
      raise ValueError('No definition found for device_type "%s"', device_type)
                      
    compiled_format = device_definition.get('compiled_format', None)
    for trial_format in compiled_format:
      fields = trial_format.parse(message)
      if fields:
        return fields.named

    # Nothing matched, go home empty-handed
    logging.warning('No formats for %s matched message %s',
                    device_type, message)
    return {}

  ############################
  def _read_definitions(self, filespec_paths):
    """Read the files on the filespec_paths and return dictinary of
    accumulated definitions.
    """
    definitions = {}
    for filespec in filespec_paths.split(','):
      filenames = glob.glob(filespec)
      if not filenames:
        logging.warning('No files match definition file spec "%s"', filespec)

      for filename in filenames:
        file_definitions = read_config.read_config(filename)

        for new_def_name, new_def in file_definitions.items():
          if new_def_name in definitions:
            logging.warning('Duplicate definition for "%s" found in %s',
                            new_def_name, filename)
          definitions[new_def_name] = new_def
    return definitions

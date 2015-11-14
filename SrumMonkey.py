# A tool to convert and analyse SRUM
#
# Copyright (C) 2015, G-C Partners, LLC <dev@g-cpartners.com>
# G-C Partners licenses this file to you under the Apache License, Version
# 2.0 (the "License"); you may not use this file except in compliance with the
# License.  You may obtain a copy of the License at:
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.  See the License for the specific language governing
# permissions and limitations under the License.
import sqlite3
import struct
import logging
import datetime
import sys
import os
import re
import argparse
import copy

#Requires Metz' libesedb
#https://github.com/libyal/libesedb
#or you can find compiled python bindings for MacOSX and Window versions at
#https://github.com/log2timeline/l2tbinaries
from pyesedb import column_types as DBTYPES
import pyesedb

#Requires installing python-registry
#https://github.com/williballenthin/python-registry
from Registry import *

def GetOptions():
    '''Get needed options for processesing'''
    
    usage = """Copywrite G-C Partners, LLC 2015"""
    
    options = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(usage)
    )
    
    ###Case Details###
    options.add_argument(
        '--srum_db',
        dest='srum_db',
        action="store",
        type=unicode,
        required=True,
        default=None,
        help='SRUM Database'
    )
    
    options.add_argument(
        '--output_db',
        dest='output_db',
        required=True,
        action="store",
        type=unicode,
        default=None,
        help='Output Database Name'
    )
    
    options.add_argument(
        '--software_hive',
        dest='software_hive',
        action="store",
        type=unicode,
        default=None,
        help='SOFTWARE Hive for Interface Enumeration'
    )
    
    return options

def Main():
    ###GET OPTIONS###
    arguements = GetOptions()
    options = arguements.parse_args()
    
    if os.path.isfile(options.output_db):
        os.remove(options.output_db)
    
    srumHandler = SrumHandler(
        options
    )
    
    srumHandler.ConvertDb()
    
    if options.software_hive is not None:
        if os.path.isfile(options.software_hive):
            #Enumerate Registry Here#
            rhandler = RegistryHandler(
                options
            )
            
            rhandler.EnumerateRegistryValues()
            
            pass
        else:
            logging.error('No such software_hive file: {}'.format(options.software_hive))
    
class RegistryHandler():
    '''Registry Operations'''
    INTERFACE_COLUMN_MAPPING = {
        'ProfileIndex':'INTEGER',
        'succeeded':'BLOB',
        'ProfileGuid':'TEXT',
        'Flags':'INTEGER',
        'All User Profile Security Descriptor':'TEXT',
        'CreatorSid':'BLOB',
        'InterfaceGuid':'TEXT',
        'SSID':'TEXT',
        'Nla':'BLOB',
        'NameLength':'INTEGER',
        'Name':'TEXT'
    }
    CUSTOM_COLUMNS = {
        'All User Profile Security Descriptor':{
            'type':'utf-16le'
        },
        'Channel Hints':{
            'type':'ChannelHints'
        }
    }
    SQLITE_TYPE = {
        'DATETIME':[
            
        ],
        'REAL':[
            
        ],
        'INTEGER':[
            Registry.RegDWord
        ],
        'BLOB':[
        ],
        'TEXT':[
        ]
    }
    
    def __init__(self,options):
        self.options = options
        hive = options.software_hive
        self.registry = Registry.Registry(
            hive
        )
        
        self.outputDbConfig = DbConfig(
            dbname=self.options.output_db
        )
        
        self.outputDbHandler = DbHandler(
            self.outputDbConfig
        )
        
        self.INTERFACE_COLUMN_LISTING = []
        
    def EnumerateRegistryValues(self):
        reg_key = self.registry.open('Microsoft\\WlanSvc\\Interfaces')
        profile_list = []
        for interface_key in reg_key.subkeys():
            #Get Interface GUID#
            interface_guid = interface_key.name()
            #If Interface Key has sub keys, enumerate profiles#
            if interface_key.subkeys_number() > 0:
                #Get Profiles Key#
                profiles_key = interface_key.subkey('Profiles')
                profile_dict = {
                    'InterfaceGuid':interface_guid
                }
                for profile_key in profiles_key.subkeys():
                    profile_guid = profile_key.name()
                    profile_dict['ProfileGuid'] = profile_guid
                    if profile_key.values_number() > 0:
                        for value in profile_key.values():
                            profile_dict[value.name()] = value.value()
                    if profile_key.subkeys_number() > 0:
                        metadata_key = profile_key.subkey('MetaData')
                        if metadata_key.values_number() > 0:
                            for value in metadata_key.values():
                                resolved_value = self._GetValue(value)
                                if isinstance(resolved_value,dict):
                                    profile_dict.update(resolved_value)
                                else:
                                    profile_dict[value.name()] = self._GetValue(value)
                    
                    for key in profile_dict:
                        if key not in self.INTERFACE_COLUMN_LISTING:
                            self.INTERFACE_COLUMN_LISTING.append(key)
                            
                    profile_list.append(copy.deepcopy(profile_dict))
                pass
            pass
        
        self.outputDbHandler.CreateTableFromMapping(
            'WlanSvcInterfaceProfiles',
            RegistryHandler.INTERFACE_COLUMN_MAPPING,
            None,
            self.INTERFACE_COLUMN_LISTING
        )
        
        self.outputDbHandler.InsertFromListOfDicts(
            'WlanSvcInterfaceProfiles',
            profile_list,
            self.INTERFACE_COLUMN_LISTING
        )
    
    def _GetValue(self,value):
        new_value = value.value()
        vname = value.name()
        vtype = value.value_type()
        
        ###CHECK FOR CUSTOM DEFINED TABLE COLUMNS TYPES###
        if vname in RegistryHandler.CUSTOM_COLUMNS:
            new_value = self._GetCustomValue(
                RegistryHandler.CUSTOM_COLUMNS[vname],
                new_value
            )
            
            return new_value
        
        return new_value
    
    def _GetCustomValue(self,custom_info,data):
        value = data
        if 'type' in custom_info:
            if custom_info['type'] == 'utf-16le':
                value = data.decode('utf-16le')
            elif custom_info['type'] == 'ChannelHints':
                value = ChannelHints(data)
            elif custom_info['type'] == 'WinDatetime':
                value = GetWinTimeStamp(data)
                
        return value
    
class SrumHandler():
    '''A Handler for converting SRU to SQLite'''
    CURRENT_LOCATION = {
        'table':None,
        'table_enum':None,
        'column':None
    }
    GUID_TABLES = {
        '{DD6636C4-8929-4683-974E-22C046A43763}':'NetworkConnectivityData',
        '{D10CA2FE-6FCF-4F6D-848E-B2E99266FA89}':'ApplicationResourceUsageData',
        '{973F5D5C-1D90-4944-BE8E-24B94231A174}':'NetworkUsageData',
        '{D10CA2FE-6FCF-4F6D-848E-B2E99266FA86}':'EnergyUsageData',
        '{FEE4E14F-02A9-4550-B5CE-5FA2DA202E37}':'WindowsPushNotificationData',
        '{FEE4E14F-02A9-4550-B5CE-5FA2DA202E37}LT':'WindowsPushNotificationDataLT',
    }
    SQLITE_TYPE = {
        'DATETIME':[
            pyesedb.column_types.DATE_TIME
        ],
        'REAL':[
            pyesedb.column_types.DOUBLE_64BIT,
            pyesedb.column_types.FLOAT_32BIT
        ],
        'INTEGER':[
            pyesedb.column_types.BOOLEAN,
            pyesedb.column_types.INTEGER_16BIT_SIGNED,
            pyesedb.column_types.INTEGER_16BIT_UNSIGNED,
            pyesedb.column_types.INTEGER_32BIT_SIGNED,
            pyesedb.column_types.INTEGER_32BIT_UNSIGNED,
            pyesedb.column_types.INTEGER_64BIT_SIGNED,
            pyesedb.column_types.INTEGER_8BIT_UNSIGNED
        ],
        'BLOB':[
            pyesedb.column_types.BINARY_DATA,
            pyesedb.column_types.LARGE_BINARY_DATA
        ],
        'TEXT':[
            pyesedb.column_types.GUID,
            pyesedb.column_types.LARGE_TEXT,
            pyesedb.column_types.SUPER_LARGE_VALUE,
            pyesedb.column_types.TEXT
        ]
    }
    
    #If Columns have same name but need to be treated differently#
    CUSTOM_TABLES = {
        
    }
    #How to decode a special column#
    CUSTOM_COLUMNS = {
        'EventTimestamp':{
            'type':'WinDatetime'
        },
        'ConnectStartTime':{
            'type':'WinDatetime'
        },
        'LocaleName':{
            'type':'utf-16le'
        },
        'Key':{
            'type':'utf-16le'
        },
        'IdBlob':{
            'type':'IdBlob'
        }
    }

    def __init__(self,options):
        self.srum_db = options.srum_db
        self.output_db = options.output_db
        
        self.esedb_file = pyesedb.file()
        self.esedb_file.open(self.srum_db)
        
        self.outputDbConfig = DbConfig(
            dbname=self.output_db
        )
        
        self.outputDbHandler = DbHandler(
            self.outputDbConfig
        )
        
    def _CreateTableNameFromGuid(self,guid):
        '''If you wanted to change the table name of a guid table'''
        new_table_name = guid
        
        #new_table_name = new_table_name.replace('{','')
        #new_table_name = new_table_name.replace('}','')
        #new_table_name = new_table_name.replace('-','')
        
        return new_table_name
        
        
    def ConvertDb(self):
        '''Convert SRU Database to a SQLite Database'''
        for table in self.esedb_file.tables:
            #Enumerate if GUID Table#
            self.table_name = table.name
            if self.table_name in SrumHandler.GUID_TABLES:
                self.table_name = SrumHandler.GUID_TABLES[self.table_name]
                
            ###Check if Table Name is GUID###
            regexp = re.compile(r'^\{[0-9a-zA-Z]{8}\-[0-9a-zA-Z]{4}\-[0-9a-zA-Z]{4}\-[0-9a-zA-Z]{4}\-[0-9a-zA-Z]{12}\}')
            if regexp.search(self.table_name) is not None:
                self.table_name = self._CreateTableNameFromGuid(
                    self.table_name
                )
            
            SrumHandler.CURRENT_LOCATION['table'] = table.name
            SrumHandler.CURRENT_LOCATION['table_enum'] = self.table_name
            
            print 'Converting Table {} as {}'.format(table.name,self.table_name)
            
            column_names = []
            for column in table.columns:
                column_names.append(column.name)
                
            self._CreateTable(
                table
            )
            
            num_of_columns = table.get_number_of_columns()
            items_to_insert = []
            for record in table.records:
                enum_record = self._EnumerateRecord(
                    num_of_columns,
                    record
                )
                items_to_insert.append(enum_record)
                
            self.outputDbHandler.InsertFromListOfDicts(
                self.table_name,
                items_to_insert,
                column_names
            )
            
    def _CreateTable(self,table):
        column_names = []
        for column in table.columns:
            column_names.append(column.name)
        
        field_mapping = self._CreateFieldMapping(
            table
        )
        
        self.outputDbHandler.CreateTableFromMapping(
            self.table_name,
            field_mapping,
            None,
            column_names
        )
        
    def _CreateFieldMapping(self,table):
        field_mapping = {}
        for column in table.columns:
            key = column.name
            
            if column.type in SrumHandler.SQLITE_TYPE['TEXT']:
                field_mapping[key] = 'TEXT'
            elif column.type in SrumHandler.SQLITE_TYPE['BLOB']:
                field_mapping[key] = 'BLOB'
            elif column.type in SrumHandler.SQLITE_TYPE['INTEGER']:
                field_mapping[key] = 'INTEGER'
            elif column.type in SrumHandler.SQLITE_TYPE['REAL']:
                field_mapping[key] = 'REAL'
            elif column.type in SrumHandler.SQLITE_TYPE['DATETIME']:
                field_mapping[key] = 'DATETIME'
            else:
                logging.error('Type not accounted for in table mapping creation: {}'.format(column.type))
                sys.exit(1)
        
        return field_mapping
    
    def _EnumerateRecord(self,num_of_columns,record):
        '''Enumerate vales for a record'''
        values = {}
        for index in range(0,num_of_columns):
            self.CURRENT_VALUES = values
            data = self._GetColumnValueFromRecord(
                record,
                index
            )
            
            values.update(data)
            
        return values
        
    def _GetColumnValueFromRecord(self,record,index):
        '''Get enumerated value based off of column or type'''
        item = {}
        value = None
        name = record.get_column_name(index)
        dtype = record.get_column_type(index)
        data = record.get_value_data(index)
        
        SrumHandler.CURRENT_LOCATION['column'] = name
        
        if data is None:
            item = {name:None}
            return item
        
        ###CHECK FOR CUSTOM DEFINED TABLE COLUMNS TYPES###
        if self.table_name in SrumHandler.CUSTOM_TABLES:
            if name in SrumHandler.CUSTOM_TABLES[self.table_name]:
                value = self._GetCustomValue(
                    SrumHandler.CUSTOM_TABLES[name][self.table_name],
                    data
                )
                item = {name:value}
                return item
            
        ###CHECK FOR CUSTOM DEFINED TABLE COLUMNS TYPES###
        if name in SrumHandler.CUSTOM_COLUMNS:
                value = self._GetCustomValue(
                    SrumHandler.CUSTOM_COLUMNS[name],
                    data
                )
                item = {name:value}
                return item
        
        if dtype == DBTYPES.DOUBLE_64BIT:
            value = struct.unpack('d',data)[0]
        if dtype == DBTYPES.FLOAT_32BIT:
            value = struct.unpack('f',data)[0]
        if dtype == DBTYPES.BOOLEAN:
            value = struct.unpack('?',data)[0]
        elif dtype == DBTYPES.INTEGER_8BIT_UNSIGNED:
            value = struct.unpack('B',data)[0]
        elif dtype == DBTYPES.INTEGER_16BIT_SIGNED:
            value = struct.unpack('h',data)[0]
        elif dtype == DBTYPES.INTEGER_16BIT_UNSIGNED:
            value = struct.unpack('H',data)[0]
        elif dtype == DBTYPES.INTEGER_32BIT_SIGNED:
            value = struct.unpack('i',data)[0]
        elif dtype == DBTYPES.INTEGER_32BIT_UNSIGNED:
            value = struct.unpack('I',data)[0]
        elif dtype == DBTYPES.INTEGER_64BIT_SIGNED:
            value = struct.unpack('q',data)[0]
        elif dtype == DBTYPES.GUID:
            value = uuid.UUID(bytes=data)
        elif dtype == DBTYPES.LARGE_TEXT:
            value = data
        elif dtype == DBTYPES.SUPER_LARGE_VALUE:
            value = data
        elif dtype == DBTYPES.TEXT:
            value = data
        elif dtype == DBTYPES.BINARY_DATA:
            value = data
        elif dtype == DBTYPES.LARGE_BINARY_DATA:
            value = data
        elif dtype == DBTYPES.DATE_TIME:
            value = GetOleTimeStamp(data)
        else:
            msg = 'UNKNOWN TYPE {}'.format(dtype)
            logging.error(msg)
            raise Exception(msg)
        
        item = {name:value}
        
        return item
    
    def _GetCustomValue(self,custom_info,data):
        value = data
        if 'type' in custom_info:
            if custom_info['type'] == 'utf-16le':
                value = data.decode('utf-16le')
            elif custom_info['type'] == 'OleDatetime':
                value = GetOleTimeStamp(data)
            elif custom_info['type'] == 'WinDatetime':
                value = GetWinTimeStamp(data)
            elif custom_info['type'] == 'IdBlob':
                if self.CURRENT_VALUES['IdType'] == 2 or self.CURRENT_VALUES['IdType'] == 1 or self.CURRENT_VALUES['IdType'] == 0:
                    value = data.decode('utf-16le')
                
        return value

    
def GetOleTimeStamp(raw_timestamp):
    timestamp = struct.unpack(
        "d",
        raw_timestamp
    )[0]
    
    origDateTime = datetime.datetime(
        1899,
        12,
        30,
        0,
        0,
        0
    )
    
    timeDelta = datetime.timedelta(days=timestamp)
    
    new_datetime = origDateTime + timeDelta
  
    dt_string = new_datetime.strftime("%Y-%m-%d %H:%M:%S.%f")
    return dt_string

def GetWinTimeStamp(raw_timestamp):
    timestamp = struct.unpack(
        "Q",
        raw_timestamp
    )[0]
    
    if datetime < 0:
        return None
    
    microsecs, _ = divmod(
        timestamp,
        10
    )
    
    timeDelta = datetime.timedelta(
        microseconds=microsecs
    )
    
    origDateTime = datetime.datetime(
        1601,
        1,
        1
    )
    
    new_datetime = origDateTime + timeDelta
    dt_string = new_datetime.strftime("%Y-%m-%d %H:%M:%S.%f")
    return dt_string

class ChannelHints(dict):
    def __init__(self,data):
        self['NameLength'] = struct.unpack("I",data[0:4])[0]
        self['Name'] = data[4:4+self['NameLength']]
        self['SSID'] = data[36:36+32].encode('hex')
        
class DbConfig():
    '''This tells the DbHandler what to connect too'''
    def __init__(self,dbname=None):
        self.db = dbname

class DbHandler():
    def __init__(self,db_config,table=None):
        #Db Flags#
        self.db_config = db_config
        
    def CreateTableFromMapping(self,tbl_name,field_mapping,primary_key_str,field_order):
        dbh = self.GetDbHandle()
        
        string = "CREATE TABLE IF NOT EXISTS '{0:s}' (\n".format(tbl_name)
        for field in field_order:
            string += "'{0:s}' {1:s},\n".format(
                field,
                field_mapping[field]
            )
        
        if primary_key_str is not None:
            string = string + primary_key_str
        else:
            string = string[0:-2]
        
        string = string + ')'
        
        cursor = dbh.cursor()
        
        cursor.execute(string)
        
    def CreateInsertString(self,table,row,column_order,INSERT_STR=None):
        nco = []
        for column in column_order:
            nco.append("'{}'".format(column))
            
        columns = ', '.join(nco)
        
        in_row = []
        
        for key in column_order:
            if key in row.keys():
                in_row.append("{}".format(row[key]))
            else:
                in_row.append(None)
            
            placeholders = ','.join('?' * len(in_row))
        
        if INSERT_STR == None:
            INSERT_STR = 'INSERT OR IGNORE'
        
        sql = '{} INTO \'{}\' ({}) VALUES ({})'.format(INSERT_STR,table,columns, placeholders)
            
        return sql
    
    def InsertFromListOfDicts(self,table,rows_to_insert,column_order,INSERT_STR=None):
        dbh = self.GetDbHandle()
        sql_c = dbh.cursor()
        
        for row in rows_to_insert:
            in_row = []
            sql = self.CreateInsertString(
                table,
                row,
                column_order,
                INSERT_STR=None
            )
            
            for key in column_order:
                if key in row.keys():
                    in_row.append(row[key])
                else:
                    in_row.append(None)
            
            try:
                sql_c.execute(sql,in_row)
            except MySQLdb.IntegrityError:
                #MySQL Duplicate on primary key#
                pass
            except Exception as e:
                print "[ERROR] {}\n[SQL] {}\n[ROW] {}".format(str(e),sql,str(row))
        
        dbh.commit()
    
    def CreateView(self,view_str):
        dbh = self.GetDbHandle()
        cursor = dbh.cursor()
        
        cursor.execute(view_str)
        dbh.commit()
    
    def GetDbHandle(self):
        '''Create database handle based off of databaseinfo'''
        dbh = None
        
        dbh = sqlite3.connect(
            self.db_config.db,
            timeout=10000
        )
        
        return dbh

if __name__ == '__main__':
    Main()
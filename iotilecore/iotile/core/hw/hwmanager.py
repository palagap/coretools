# This file is adapted from python code released by WellDone International
# under the terms of the LGPLv3.  WellDone International's contact information is
# info@welldone.org
# http://welldone.org
#
# Modifications to this file from the original created at WellDone International 
# are copyright Arch Systems Inc.
import pkg_resources

from iotile.core.utilities.typedargs import *
from Queue import Empty
from iotile.core.hw.transport import *
from iotile.core.hw.exceptions import *
from iotile.core.exceptions import *
from iotile.core.utilities.typedargs.annotate import param, return_type, finalizer
from iotile.core.hw.proxy.proxy import TileBusProxyObject
from iotile.core.dev.registry import ComponentRegistry
from iotile.core.hw.transport.adapterstream import AdapterCMDStream
from iotile.core.dev.config import ConfigManager
import inspect
import os.path
import imp
import binascii
import sys


@context("HardwareManager")
class HardwareManager:
    """
    A module for managing and interacting with IOTile Hardware

    This context provides tools to configure, control, debug and program
    any IOTile module.  Specific functionality can be implemented in dynamically
    loaded proxy objects that are designed to provide access to each IOTile.

    To create a HardwareManager, you need to pass a port string that describes the 
    method to be used to connect to the IOTile device. The method should specify the 
    name of the connection method optionally followed by a colon and any extra information
    possibly needed to connect using that method.
    """

    @param("port", "string", desc="transport method to use in the format transport[:port[,connection_string]]")
    @param("record", "path", desc="Optional file to record all RPC calls and responses made on this HardwareManager")
    def __init__(self, port=None, record=None):
        if port is None:
            try:
                conf = ConfigManager()
                port = conf.get('core:default-port')
            except ArgumentError:
                raise ArgumentError("No port given and no core:default-port config variable set", suggestion="Specify the port to use to connect to the IOTile devices")

        transport, _, arg = port.partition(':')

        self.transport = transport
        self.port = None
        if arg != "":
            self.port = arg

        self.record = record

        self.stream = self._create_stream()
        self._stream_queue = None
        self._trace_queue = None
        self._trace_data = bytearray()
        self.proxies = {'TileBusProxyObject': TileBusProxyObject}
        self.name_map = {TileBusProxyObject.ModuleName(): [TileBusProxyObject]}

        self._setup_proxies()

    def _setup_proxies(self):
        """
        Load in proxy module objects for all of the registered components on this system
        """

        # Find all of the registered IOTile components and see if we need to add any proxies for them
        reg = ComponentRegistry()
        modules = reg.list_components()

        proxies = reduce(lambda x, y: x+y, [reg.find_component(x).proxy_modules() for x in modules], [])
        for prox in proxies:
            self.add_proxies(prox)

        # Find all installed proxy objects through registered entry points
        for entry in pkg_resources.iter_entry_points('iotile.proxy'):
            mod = entry.load()
            self._add_proxy_module(mod)

    @param("address", "integer", "positive", desc="numerical address of module to get")
    def get(self, address):
        """
        Create a proxy object for a tile by address.

        The correct proxy object is determined by asking the tile for its status information
        and looking up the appropriate proxy in our list of installed proxy objects
        """

        tile = self._create_proxy('TileBusProxyObject', address)
        name = tile.tile_name()
        version = tile.tile_version()

        # Now create the appropriate proxy object based on the name and version of the tile 
        tile_type = self.get_proxy(name)
        if tile_type is None:
            raise HardwareError("Could not find proxy object for tile", name="'{}'".format(name), known_names=self.name_map.keys())

        tile = tile_type(self.stream, address)      
        return tile

    @annotated
    def controller(self):
        """
        Find an attached IOTile controller and attempt to connect to it.
        """

        con = self.get(8)       
        con._hwmanager = self

        return con

    @param("device_uuid", "integer", desc="UUID of the device we would like to connect to")
    def connect(self, device_uuid):
        """Attempt to connect to a device by its UUID
        """

        self.stream.connect(device_uuid)

    @param("connection_string", "string", desc="opaque connection string indicating which device")
    def connect_direct(self, connection_string):
        """Attempt to connect to a device using a connection string
        """

        self.stream.connect_direct(connection_string)

    @annotated
    def disconnect(self):
        """Attempt to disconnect from a device 
        """

        self.stream.disconnect()

    @return_type("bool")
    def heartbeat(self):
        """
        Check whether the underlying command stream is functional
        """

        return self.stream.heartbeat()

    @annotated
    def enable_streaming(self):
        """
        Enable streaming of sensor graph data over this interface
        """

        self._stream_queue = self.stream.enable_streaming()

    @annotated
    def enable_tracing(self):
        """Enable tracing of realtime debug information over this interface
        """

        self._trace_queue = self.stream.enable_tracing()

    @return_type("integer")
    def count_reports(self):
        if self._stream_queue is None:
            return 0

        return self._stream_queue.qsize()

    @return_type("string")
    @param("encoding", "string", desc="The encoding to use to dump the trace, either 'hex' or 'raw'")
    def dump_trace(self, encoding):
        """Dump all received tracing data currently received from the device to stdout

        The data is encoded per the encoding parmeter which must be either
        the string 'hex' or 'raw'.  If hex is passed, the data is printed as hex digits,
        if raw is passed, the data is printed as received from the device.
        """

        if encoding not in ['raw', 'hex']:
            raise ValidationError("Unknown encoding type specified in dump trace", encoding=encoding, known_encodings=['hex', 'raw'])

        if self._trace_queue is None:
            return ""

        self._accumulate_trace()

        if encoding == 'raw':
            return str(self._trace_data)

        return binascii.hexlify(self._trace_data)

    def _accumulate_trace(self):
        """Copy tracing data from trace queue into _trace_data
        """

        if self._trace_queue is None:
            return

        try:
            while True:
                blob = self._trace_queue.get(block=False)
                self._trace_data += bytearray(blob)
        except Empty:
            pass

    def iter_reports(self, blocking=False):
        """Iterate over reports that have been received

        If blocking is True, this iterator will never stop.  Otherwise
        it will iterate over all reports currently in the queue (and those
        added during iteration)

        Args:
            blocking (bool): Whether to stop when there are no more readings or
                block and wait for more.
        """
        if self._stream_queue is None:
            return

        try:
            while True:
                yield self._stream_queue.get(block=blocking)
        except Empty:
            pass

    def wait_reports(self, num_reports, timeout=2.0):
        """Wait for a fixed number of reports to be received

        Args:
            num_reports (int): The number of reports to wait for
            timeout (float): The maximum number of seconds to wait without
                receiving another report.
        """

        if self._stream_queue is None:
            raise EnvironmentError("You have to enable streaming before you can wait for reports")

        reports = []

        for i in xrange(0, num_reports):
            try:
                report = self._stream_queue.get(timeout=timeout)
                reports.append(report)
            except Empty:
                raise TimeoutError("Timeout waiting for a report", expected_number=num_reports, received_number=i, received_reports=reports)

        return reports

    @annotated
    def reset(self):
        """
        Attempt to reset the underlying stream back to a known state
        """

        self.stream.reset()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.stream.close()
        return False

    @finalizer
    def close(self):
        self.stream.close()

    @param("path", "path", "readable", desc="path to module to load")
    @return_type("integer")
    def add_proxies(self, path):
        """
        Add all proxy objects defined in the python module located at path.

        The module is loaded and all classes that inherit from TileBusProxyObject
        are loaded and can be used to interact later with modules of that type.

        Returns the total number of proxies added.
        """

        d,p = os.path.split(path)

        p,ext = os.path.splitext(p)
        if ext != '.py' and ext != '.pyc' and ext != "":
            raise ArgumentError("Passed proxy module is not a python package or module (.py or .pyc)", path=path)

        try:
            fileobj,pathname,description = imp.find_module(p, [d])

            #Don't load modules twice
            if p in sys.modules:
                mod = sys.modules[p]
            else:
                mod = imp.load_module(p, fileobj, pathname, description)
        except ImportError as e:
            raise ArgumentError("could not import module in order to load external proxy modules", module_path=path, parent_directory=d, module_name=p, error=str(e))

        return self._add_proxy_module(mod)

    def _add_proxy_module(self, mod):
        """Add a proxy module that has already been imported

        Args:
            mod (module): A python module object that may contain a TileBusProxyObject subclass

        Returns:
            integer: The number of new TilebusProxyObject classes found
        """

        num_added = 0
        for obj in filter(lambda x: inspect.isclass(x) and issubclass(x, TileBusProxyObject) and x != TileBusProxyObject, mod.__dict__.itervalues()):
            if obj.__name__ in self.proxies:
                continue #Don't readd proxies that we already know about

            self.proxies[obj.__name__] = obj

            #Check if this object matches a specific shortened name so that we can 
            #automatically match a hw module to a proxy without user intervention
            if hasattr(obj, 'ModuleName'):
                short_name = obj.ModuleName()
                if short_name in self.name_map:
                    self.name_map[short_name].append(obj)
                else:
                    self.name_map[short_name] = [obj]

            num_added += 1

        return num_added

    @annotated
    @return_type("list(basic_dict)")
    def scan(self):
        """
        Scan for available devices and print a dictionary of information about them
        """

        devices = self.stream.scan()

        #FIXME: Use dictionary format in bled112stream to document information returned about devices
        return devices

    def get_proxy(self, short_name):
        """
        Find a proxy type given its short name.

        If no proxy type is found, return None.
        """

        if short_name not in self.name_map:
            return None

        return self.name_map[short_name][0]

    def _create_proxy(self, proxy, address):
        """
        Create a python proxy object to talk to an IOTile module with the given type
        at the given address.
        """

        if proxy not in self.proxies:
            raise UnknownModuleTypeError("unknown proxy module specified", module_type=proxy, known_types=self.proxies.keys())

        proxy_class = self.proxies[proxy]
        return proxy_class(self.stream, address)

    def _create_stream(self):
        conn_string = None
        port = self.port
        if port is not None and "," in port:
            port, conn_string = port.split(',')

        if port is not None:
            port = port.strip()
        if conn_string is not None:
            conn_string = conn_string.strip()

        #First check if this is the special none stream that creates a transport channel nowhere
        if self.transport == 'none':
            return CMDStream(port, conn_string, record=self.record)

        #Next attempt to find a CMDStream that is registered for this transport type
        for stream_entry in pkg_resources.iter_entry_points('iotile.cmdstream'):
            if stream_entry.name != self.transport:
                continue

            stream_factory = stream_entry.load()
            return stream_factory(port, conn_string, record=self.record)

        #Otherwise attempt to find a DeviceAdapter that we can turn into a CMDStream
        for adapter_entry in pkg_resources.iter_entry_points('iotile.device_adapter'):
            if adapter_entry.name != self.transport:
                continue

            adapter_factory = adapter_entry.load()
            return AdapterCMDStream(adapter_factory(port), port, conn_string, record=self.record)

        raise HardwareError("Could not find transport object registered to handle passed transport type", transport=self.transport)

import argparse
import pkg_resources
import sys
import logging
import json
from iotile.core.exceptions import ArgumentError, IOTileException
from iotile.core.hw.hwmanager import HardwareManager

def main():
    """Run a script that puts an IOTile device into a known state
    """

    list_parser = argparse.ArgumentParser(add_help=False)
    list_parser.add_argument('-l', '--list', action='store_true', help="List all known device preparation scripts and then exit")

    parser = argparse.ArgumentParser(description="Prepare a device or a list of devices into a known state using a script")
    
    parser.add_argument('port', help="The name of the port to use to connect to the device")
    parser.add_argument('script', help="The name of the device preparation script to use (can either be an installed script name or a .py file with extension")
    parser.add_argument('-c', '--config', help="An optional JSON config file with arguments for the script")
    parser.add_argument('-l', '--list', action='store_true', help="List all known scripts and then exit")
    parser.add_argument('--uuid', action='append', default=[], help="Run script on device given by this uuid")
    parser.add_argument('--device', action='append', default=[], help="Run script on device given by this connection string")
    parser.add_argument('--pause', action='store_true', help="Pause and wait for user input after finishing each device")
    parser.add_argument('--max-retries', type=int, default=5, help="Number of times to retry (up to a max of 5 times)")
    parser.add_argument('--uuid-range', action='append', help="Process every device in a range (range should be specified as start-end and is inclusive, e.g ab-cd)")
    args, rest = list_parser.parse_known_args()

    if args.list:
        print("\nInstalled Preparation Scripts:")
        for entry in pkg_resources.iter_entry_points('iotile.device_recipe'):
            print('- {}'.format(entry.name))

        return 0

    args = parser.parse_args()

    config = {}
    iface = None
    if args.config is not None:
        with open(args.config, "rb") as conf_file:
            config = json.load(conf_file)

    script, env = instantiate_script(args.script)

    success = []

    devices = []
    devices.extend([('uuid', int(x, 16)) for x in args.uuid])
    devices.extend([('conection_string', x) for x in args.device])

    for uuid_range in args.uuid_range:
        start,_,end = uuid_range.partition('-')
        start = int(start, 16)
        end = int(end, 16)

        devices.extend([('uuid', x) for x in xrange(start, end+1)])

    try:
        with HardwareManager(port=args.port) as hw:
            for conntype, dev in devices:
                for i in xrange(0, args.max_retries): 
                    try:
                        print("Configuring device %s identified by %s" % (str(dev), conntype))
                        configure_device(hw, conntype, dev, script, config)
                        break
                    except IOTileException, exc:
                        print("--> Error on try %d: %s" % (i+1, str(exc)))

                success.append((conntype, dev))
                if args.pause:
                    raw_input("--> Waiting for <return> before processing next device")
    except KeyboardInterrupt:
        print("Break received, cleanly exiting...")

    print("\n**FINISHED**\n")
    print("Successfully processed %d devices" % len(success))
    for conntype,conn in success:
        print("%s: %s" % (conntype, conn))

def configure_device(hw, conntype, conarg, script, args):
    if conntype == 'uuid':
        hw.connect(conarg)
    else:
        hw.connect_direct(conarg)

    try:
        script(hw, args)
    finally:
        hw.disconnect()

def instantiate_script(device_recipe):
    """Find a device recipe by name and instantiate it

    Args:
        device_recipe (string): The name of the pkg_resources entry point corresponding to
            the device.  It should be in group iotile.device_recipe

    Returns:
        tuple(callable, dict): A callable function with signature callable(HWManager, config_dict) that
            executes the script and a dictionary that must be kept around as long as callable is.
    """

    if device_recipe.endswith('.py'):
        env = {}
        # Use execfile rather than importing as a module since that could collide with imported module names
        execfile(device_recipe, env)

        if 'main' not in env:
            raise ArgumentError("Could not find main() function in passed device script", script_file=device_recipe)

        return env['main'], env

    for entry in pkg_resources.iter_entry_points('iotile.device_recipe', name=device_recipe):
        dev = entry.load()
        return dev, {}

    print("Could not find an installed device preparation script with the given name: {}".format(device_recipe))
    sys.exit(1)
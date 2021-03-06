"""Standalone Receiver"""
import logging, os, time, ipaddress
from argparse import ArgumentParser, ArgumentDefaultsHelpFormatter
from . import rp, firewall, defs, config, dependencies, util, monitoring
from pysnmp.hlapi import *
logger = logging.getLogger(__name__)


class SnmpClient():
    """SNMP Client"""
    def __init__(self, address, port, mib):
        """Constructor

        Args:
            address : SNMP agent's IP address
            port    : SNMP agent's port
            mib     : Target SNMP MIB

        """
        assert(ipaddress.ip_address(address))  # parse address
        self.address = address
        self.port    = port
        self.mib     = mib
        self._dump_mib()

    def _dump_mib(self):
        """Generate the compiled (.py) MIB file"""
        sudo_user = os.environ.get('SUDO_USER')
        user      = sudo_user if sudo_user is not None else ""
        home      = os.path.expanduser("~" + user)

        # Check if the compiled MIB (.py file) already exists
        mib_dir = os.path.join(home, ".pysnmp/mibs/")
        if (os.path.exists(os.path.join(mib_dir, self.mib + ".py"))):
            return

        cli_dir  = os.path.dirname(os.path.abspath(__file__))
        mib_path = os.path.join(cli_dir, "mib")
        cmd = ["mibdump.py",
               "--mib-source={}".format(mib_path),
               self.mib]
        util.run_and_log(cmd, logger=logger)

    def _get(self, *variables):
        """Get one or more variables via SNMP

        Args:
            Tuple with the variables to fetch via SNMP.

        Returns:
            List of tuples with the fetched keys and values.

        """
        obj_types = []
        for var in variables:
            if isinstance(var, tuple):
                obj = ObjectType(ObjectIdentity(self.mib, var[0], var[1]))
            else:
                obj = ObjectType(ObjectIdentity(self.mib, var, 0))
            obj_types.append(obj)

        errorIndication, errorStatus, errorIndex, varBinds = next(
            getCmd(SnmpEngine(),
                   CommunityData('public'),
                   UdpTransportTarget((self.address, self.port)),
                   ContextData(),
                   *obj_types
            )
        )

        if errorIndication:
            logger.error(errorIndication)
        elif errorStatus:
            logger.error('%s at %s' % (
                errorStatus.prettyPrint(),
                errorIndex and varBinds[int(errorIndex) - 1][0] or '?')
            )
        else:
            res = list()
            for varBind in varBinds:
                logger.debug(' = '.join([x.prettyPrint() for x in varBind]))
                res.append(tuple([x.prettyPrint() for x in varBind]))
            return res

    def _set(self, variable, value):
        """Set variable via SNMP

        Args:
            variable : variable to set via SNMP.
            value    : value to set on the given variable.

        """
        errorIndication, errorStatus, errorIndex, varBinds = next(
            setCmd(SnmpEngine(),
                   CommunityData('public'),
                   UdpTransportTarget((self.address, self.port)),
                   ContextData(),
                   ObjectType(ObjectIdentity(self.mib, variable, 1), value)
            )
        )

        if errorIndication:
            logger.error(errorIndication)
        elif errorStatus:
            logger.error('%s at %s' % (
                errorStatus.prettyPrint(),
                errorIndex and varBinds[int(errorIndex) - 1][0] or '?')
            )
        else:
            for varBind in varBinds:
                logger.debug(' = '.join([x.prettyPrint() for x in varBind]))


class S400Client(SnmpClient):
    """Novra S400 SNMP Client"""
    def __init__(self, demod, address, port, mib):
        super().__init__(address, port, mib)
        self.demod = demod

    def get_stats(self):
        """Get demodulator statistics

        Returns:
            Dictionary with the receiver stats following the format expected by
            the Monitor class (from monitor.py), i.e., each dictionary element
            as a tuple "(value, unit)".

        """
        res = self._get(
            's400SignalLockStatus' + self.demod,
            's400SignalStrength' + self.demod,
            's400CarrierToNoise' + self.demod,
            's400UncorrectedPackets' + self.demod,
            's400BER' + self.demod
        )

        if res is None:
            return

        signal_lock_raw  = res[0][1]
        signal_raw       = res[1][1]
        c_to_n_raw       = res[2][1]
        uncorr_raw       = res[3][1]
        ber_raw          = res[4][1]

        # Parse
        signal_lock  = (signal_lock_raw == 'locked')
        stats        = {
            'lock'  : (signal_lock, None)
        }

        # Metrics that require locking
        #
        # NOTE: the S400 does not return the signal level if unlocked.
        if (signal_lock):
            level            = float('nan') if (signal_raw == '< 70') else float(signal_raw)
            cnr              = float('nan') if (c_to_n_raw == '< 3') else float(c_to_n_raw)
            stats['snr']     = (cnr, "dB")
            stats['level']   = (level, "dBm")
            stats['ber']     = (float(ber_raw), None)
            stats['pkt_err'] = (int(uncorr_raw), None)

        return stats

    def print_demod_config(self):
        """Get demodulator configurations via SNMP

        Returns:
            Bool indicating whether the demodulator configurations were printed
            successfully.

        """
        res = self._get(
            's400FirmwareVersion',
            # Demodulator
            's400ModulationStandard' + self.demod,
            's400LBandFrequency' + self.demod,
            's400SymbolRate' + self.demod,
            's400Modcod' + self.demod,
            # LNB
            's400LNBSupply',
            's400LOFrequency',
            's400Polarization',
            's400Enable22KHzTone',
            's400LongLineCompensation',
            # MPE
            ('s400MpePid1Pid', 0),
            ('s400MpePid1Pid', 1),
            ('s400MpePid1RowStatus', 0),
            ('s400MpePid1RowStatus', 1)
        )

        if (res is None):
            return False

        # Form dictionary with the S400 configs
        cfg = {}
        for res in res:
            key = res[0].replace('NOVRA-s400-MIB::s400', '')
            val = res[1]
            cfg[key] = val

        # Map dictionary to more informative labels
        demod_label_map = {
            'ModulationStandard' + self.demod + '.0' : "Standard",
            'LBandFrequency' + self.demod + '.0' : "L-band Frequency",
            'SymbolRate' + self.demod + '.0' : "Symbol Rate",
            'Modcod' + self.demod + '.0' : "MODCOD",
        }
        lnb_label_map = {
            'LNBSupply.0' : "LNB Power Supply",
            'LOFrequency.0' : "LO Frequency",
            'Polarization.0' : "Polarization",
            'Enable22KHzTone.0' : "22 kHz Tone",
            'LongLineCompensation.0' : "Long Line Compensation"
        }
        mpe_label_map = {
            'MpePid1Pid.0'       : "MPE PID 1",
            'MpePid1Pid.1'       : "MPE PID 2",
            'MpePid1RowStatus.0' : "MPE PID 1 Status",
            'MpePid1RowStatus.1' : "MPE PID 2 Status"
        }
        label_map = {
            "Demodulator" : demod_label_map,
            "LNB Options" : lnb_label_map,
            "MPE Options" : mpe_label_map
        }

        print("Firmware Version: {}".format(cfg['FirmwareVersion.0']))
        for map_key in label_map:
            print("{}:".format(map_key))
            for key in cfg:
                if key in label_map[map_key]:
                    label = label_map[map_key][key]
                    if (label == "MODCOD"):
                        val = "VCM" if cfg[key] == "31" else cfg[key]
                    elif (label == "Standard"):
                        val = cfg[key].upper()
                    else:
                        val = cfg[key]
                    print("- {}: {}".format(label, val))
        return True


def subparser(subparsers):
    p = subparsers.add_parser('standalone',
                              description="Standalone DVB-S2 receiver manager",
                              help='Manage the standalone DVB-S2 receiver',
                              formatter_class=ArgumentDefaultsHelpFormatter)
    p.add_argument('-a', '--address',
                   default="192.168.1.2",
                   help="Address of the receiver's SNMP agent")
    p.add_argument('-p', '--port',
                   default=161,
                   type=int,
                   help="Port of the receiver's SNMP agent")
    p.add_argument('-d', '--demod',
                   default="1",
                   choices=["1","2"],
                   help="Target demodulator within the S400")
    p.set_defaults(func=print_help)

    subsubparsers = p.add_subparsers(title='subcommands',
                                     help='Target sub-command')

    # Configuration
    p1 = subsubparsers.add_parser('config', aliases=['cfg'],
                                  description='Initial configurations',
                                  help='Configure the host to receive data \
                                  from the standalone receiver')
    p1.add_argument('-i', '--interface',
                    default=None,
                    help='Network interface connected to the standalone \
                    receiver')
    p1.add_argument('-y', '--yes', default=False, action='store_true',
                    help="Default to answering Yes to configuration prompts")
    p1.set_defaults(func=cfg_standalone)

    # Monitoring
    p2 = subsubparsers.add_parser('monitor',
                                  description="Monitor the standalone receiver",
                                  help='Monitor the standalone receiver',
                                  formatter_class=ArgumentDefaultsHelpFormatter)
    # Add the default monitoring options used by other modules
    monitoring.add_to_parser(p2)
    p2.set_defaults(func=monitor)

    return p


def cfg_standalone(args):
    """Configure the host to communicate with the the standalone DVB-S2 receiver
    """
    # User info
    user_info = config.read_cfg_file(args.cfg, args.cfg_dir)

    if (user_info is None):
        return

    if 'netdev' not in user_info['setup']:
        assert(args.interface is not None), \
            ("Please specify the network interface through option "
             "\"-i/--interface\"")

    interface = args.interface if (args.interface is not None) else \
                user_info['setup']['netdev']

    # Check if all dependencies are installed
    if (not dependencies.check_apps(["iptables"])):
        return

    rp.set_filters([interface], prompt=(not args.yes))
    firewall.configure([interface], defs.src_ports, user_info['sat']['ip'],
                       igmp=True, prompt=(not args.yes))


def monitor(args):
    """Monitor the standalone DVB-S2 receiver"""

    # User info
    user_info = config.read_cfg_file(args.cfg, args.cfg_dir)

    # Client to the S400's SNMP agent
    s400 = S400Client(args.demod, args.address, args.port, mib='NOVRA-s400-MIB')

    util._print_header("Novra S400 Receiver")

    if (not s400.print_demod_config()):
        logger.error("s400 receiver at {}:{} is unreachable".format(
            s400.address, s400.port))
        return

    # Log Monitoring
    monitor = monitoring.Monitor(
        args.cfg_dir,
        logfile = args.log_file,
        scroll = args.log_scrolling,
        min_interval = args.log_interval,
        server = args.monitoring_server,
        port = args.monitoring_port,
        report = args.report,
        report_opts = {
            'dest'     : args.report_dest,
            'region'   : user_info['sat']['alias'],
            'hostname' : args.report_hostname,
            'tls_cert' : args.report_cert,
            'tls_key'  : args.report_key
        }
    )

    util._print_header("Receiver Monitoring")

    # Fetch the receiver stats periodically
    c_time = time.time()
    while (True):
        try:
            stats = s400.get_stats()

            if (stats is None):
                return

            monitor.update(stats)

            next_print = c_time + args.log_interval
            if (next_print > c_time):
                time.sleep(next_print - c_time)
            c_time = time.time()

        except KeyboardInterrupt:
            break


def print_help(args):
    """Re-create argparse's help menu for the standalone command"""
    parser     = ArgumentParser()
    subparsers = parser.add_subparsers(title='', help='')
    parser     = subparser(subparsers)
    print(parser.format_help())


"""
Library to perform pre/post test setup for virt test.
"""
import os
import logging
import time
import re
import random
import math
import shutil
import platform
import netaddr

from abc import ABCMeta
from abc import abstractmethod

from avocado.utils import process
from avocado.utils import archive
from avocado.utils import wait
from avocado.utils import genio
from avocado.utils import path
from avocado.utils import distro
from avocado.core import exceptions

from . import data_dir
from . import error_context
from . import utils_misc
from . import versionable_class
from . import openvswitch
from . import remote
from . import utils_libvirtd
from . import utils_config
from .staging import service
from .staging import utils_memory

ARCH = platform.machine()


class THPError(Exception):

    """
    Base exception for Transparent Hugepage setup.
    """
    pass


class THPNotSupportedError(THPError):

    """
    Thrown when host does not support transparent hugepages.
    """
    pass


class THPWriteConfigError(THPError):

    """
    Thrown when host does not support transparent hugepages.
    """
    pass


class THPKhugepagedError(THPError):

    """
    Thrown when khugepaged is not behaving as expected.
    """
    pass


class PolkitConfigError(Exception):

    """
    Base exception for Polkit Config setup.
    """
    pass


class PolkitRulesSetupError(PolkitConfigError):

    """
    Thrown when setup polkit rules is not behaving as expected.
    """
    pass


class PolkitWriteLibvirtdConfigError(PolkitConfigError):

    """
    Thrown when setup libvirtd config file is not behaving as expected.
    """
    pass


class PolkitConfigCleanupError(PolkitConfigError):

    """
    Thrown when polkit config cleanup is not behaving as expected.
    """
    pass


class Setuper(object):

    """
    Virtual base abstraction of setuper.
    """

    __metaclass__ = ABCMeta

    #: Skip the cleanup when error occurs
    skip_cleanup_on_error = False

    def __init__(self, test, params, env):
        """
        Initialize the setuper.

        :param test: VirtTest instance.
        :param params: Dictionary with the test parameters.
        :param env: Dictionary with test environment.
        """
        self.test = test
        self.params = params
        self.env = env

    @abstractmethod
    def setup(self):
        """Setup procedure."""
        raise NotImplementedError

    @abstractmethod
    def cleanup(self):
        """Cleanup procedure."""
        raise NotImplementedError


class SetupManager(object):

    """
    Setup Manager implementation.

    The instance can help do the setup stuff before test started and
    do the cleanup stuff after test finished. This setup-cleanup
    combined stuff will be performed in LIFO order.
    """

    def __init__(self):
        self.__setupers = []
        self.__setup_args = None

    def initialize(self, test, params, env):
        """
        Initialize the setup manager.

        :param test: VirtTest instance.
        :param params: Dictionary with the test parameters.
        :param env: Dictionary with test environment.
        """
        self.__setup_args = (test, params, env)

    def register(self, setuper_cls):
        """
        Register the given setuper class to the manager.

        :param setuper_cls: Setuper class.
        """
        if not self.__setup_args:
            raise RuntimeError("Tried to register setuper "
                               "without initialization")
        if not issubclass(setuper_cls, Setuper):
            raise ValueError("Not supported setuper class")
        self.__setupers.append(setuper_cls(*self.__setup_args))

    def do_setup(self):
        """Do setup stuff."""
        for index, setuper in enumerate(self.__setupers, 1):
            try:
                setuper.setup()
            except Exception:
                if setuper.skip_cleanup_on_error:
                    index -= 1
                # Truncate the list to prevent performing cleanup
                # for the setuper without having performed setup
                self.__setupers = self.__setupers[:index]
                raise

    def do_cleanup(self):
        """
        Do cleanup stuff.

        :return: Errors occurred in cleanup procedures.
        """
        errors = []
        while self.__setupers:
            try:
                self.__setupers.pop().cleanup()
            except Exception as err:
                logging.error(err)
                errors.append(err)
        return errors


class TransparentHugePageConfig(object):

    def __init__(self, test, params):
        """
        Find paths for transparent hugepages and kugepaged configuration. Also,
        back up original host configuration so it can be restored during
        cleanup.
        """
        self.params = params

        RH_THP_PATH = "/sys/kernel/mm/redhat_transparent_hugepage"
        UPSTREAM_THP_PATH = "/sys/kernel/mm/transparent_hugepage"
        if os.path.isdir(RH_THP_PATH):
            self.thp_path = RH_THP_PATH
        elif os.path.isdir(UPSTREAM_THP_PATH):
            self.thp_path = UPSTREAM_THP_PATH
        else:
            raise THPNotSupportedError("System doesn't support transparent "
                                       "hugepages")

        tmp_list = []
        test_cfg = {}
        test_config = self.params.get("test_config", None)
        if test_config is not None:
            tmp_list = re.split(';', test_config)
        while len(tmp_list) > 0:
            tmp_cfg = tmp_list.pop()
            test_cfg[re.split(":", tmp_cfg)[0]] = re.split(":", tmp_cfg)[1]
        # Save host current config, so we can restore it during cleanup
        # We will only save the writeable part of the config files
        original_config = {}
        # List of files that contain string config values
        self.file_list_str = []
        # List of files that contain integer config values
        self.file_list_num = []
        logging.info("Scanning THP base path and recording base values")
        for f in os.walk(self.thp_path):
            base_dir = f[0]
            if f[2]:
                for name in f[2]:
                    f_dir = os.path.join(base_dir, name)
                    parameter = file(f_dir, 'r').read()
                    logging.debug("Reading path %s: %s", f_dir,
                                  parameter.strip())
                    try:
                        # Verify if the path in question is writable
                        f = open(f_dir, 'w')
                        f.close()
                        if re.findall("\[(.*)\]", parameter):
                            original_config[f_dir] = re.findall("\[(.*)\]",
                                                                parameter)[0]
                            self.file_list_str.append(f_dir)
                        else:
                            original_config[f_dir] = int(parameter)
                            self.file_list_num.append(f_dir)
                    except IOError:
                        pass

        self.test_config = test_cfg
        self.original_config = original_config

    def set_env(self):
        """
        Applies test configuration on the host.
        """
        if self.test_config:
            logging.info("Applying custom THP test configuration")
            for path in self.test_config.keys():
                logging.info("Writing path %s: %s", path,
                             self.test_config[path])
                file(path, 'w').write(self.test_config[path])

    def value_listed(self, value):
        """
        Get a parameters list from a string
        """
        value_list = []
        for i in re.split("\[|\]|\n+|\s+", value):
            if i:
                value_list.append(i)
        return value_list

    def khugepaged_test(self):
        """
        Start, stop and frequency change test for khugepaged.
        """
        def check_status_with_value(action_list, file_name):
            """
            Check the status of khugepaged when set value to specify file.
            """
            for (act, ret) in action_list:
                logging.info("Writing path %s: %s, expected khugepage rc: %s ",
                             file_name, act, ret)
                try:
                    file_object = open(file_name, "w")
                    file_object.write(act)
                    file_object.close()
                except IOError, error_detail:
                    logging.info("IO Operation on path %s failed: %s",
                                 file_name, error_detail)
                timeout = time.time() + 50
                while time.time() < timeout:
                    try:
                        process.run('pgrep khugepaged', verbose=False)
                        if ret != 0:
                            time.sleep(1)
                            continue
                    except process.CmdError:
                        if ret == 0:
                            time.sleep(1)
                            continue
                    break
                else:
                    if ret != 0:
                        raise THPKhugepagedError("Khugepaged still alive when"
                                                 "transparent huge page is "
                                                 "disabled")
                    else:
                        raise THPKhugepagedError("Khugepaged could not be set to"
                                                 "status %s" % act)

        logging.info("Testing khugepaged")
        for file_path in self.file_list_str:
            action_list = []
            if re.findall("enabled", file_path):
                # Start and stop test for khugepaged
                value_list = self.value_listed(open(file_path, "r").read())
                for i in value_list:
                    if re.match("n", i, re.I):
                        action_stop = (i, 256)
                for i in value_list:
                    if re.match("[^n]", i, re.I):
                        action = (i, 0)
                        action_list += [action_stop, action, action_stop]
                action_list += [action]

                check_status_with_value(action_list, file_path)
            else:
                value_list = self.value_listed(open(file_path, "r").read())
                for i in value_list:
                    action = (i, 0)
                    action_list.append(action)
                check_status_with_value(action_list, file_path)

        for file_path in self.file_list_num:
            action_list = []
            file_object = open(file_path, "r")
            value = file_object.read()
            value = int(value)
            file_object.close()
            if value != 0 and value != 1:
                new_value = random.random()
                action_list.append((str(int(value * new_value)), 0))
                action_list.append((str(int(value * (new_value + 1))), 0))
            else:
                action_list.append(("0", 0))
                action_list.append(("1", 0))

            check_status_with_value(action_list, file_path)

    def setup(self):
        """
        Configure host for testing. Also, check that khugepaged is working as
        expected.
        """
        self.set_env()
        self.khugepaged_test()

    def cleanup(self):
        """:
        Restore the host's original configuration after test
        """
        logging.info("Restoring host's original THP configuration")
        for path in self.original_config:
            logging.info("Writing path %s: %s", path,
                         self.original_config[path])
            try:
                p_file = open(path, 'w')
                p_file.write(str(self.original_config[path]))
                p_file.close()
            except IOError, error_detail:
                logging.info("IO operation failed on file %s: %s", path,
                             error_detail)


class HugePageConfig(object):

    def __init__(self, params):
        """
        Gets environment variable values and calculates the target number
        of huge memory pages.

        :param params: Dict like object containing parameters for the test.
        """
        self.vms = len(params.objects("vms"))
        self.mem = int(params.get("mem"))
        self.max_vms = int(params.get("max_vms", 0))
        self.qemu_overhead = int(params.get("hugepages_qemu_overhead", 128))
        self.deallocate = params.get("hugepages_deallocate", "yes") == "yes"
        self.hugepage_path = '/mnt/kvm_hugepage'
        if os.path.exists('/proc/sys/vm/nr_hugepages'):
            self.kernel_hp_file = '/proc/sys/vm/nr_hugepages'
        else:
            raise exceptions.TestSkipError("System doesn't support hugepages")
        self.pool_path = "/sys/kernel/mm/hugepages"
        self.sys_node_path = "/sys/devices/system/node"
        # Unit is KB as default for hugepage size.
        try:
            self.expected_hugepage_size = int(
                params.get("expected_hugepage_size", 0))
        except TypeError:
            logging.warn("Invalid value 'expected_hugepage_size=%s'",
                         params.get("expected_hugepage_size"))
            self.expected_hugepage_size = 0
        self.hugepage_cpu_flag = params.get("hugepage_cpu_flag")
        self.hugepage_match_str = params.get("hugepage_match_str")
        if self.hugepage_cpu_flag and self.hugepage_match_str:
            self.check_hugepage_support()
        self.hugepage_size = self.get_hugepage_size()
        if self.expected_hugepage_size:
            self.check_hugepage_size_as_expected()
        self.hugepage_force_allocate = params.get("hugepage_force_allocate",
                                                  "no")
        self.suggest_mem = None
        self.lowest_mem_per_vm = int(params.get("lowest_mem", "256"))

        target_hugepages = params.get("target_hugepages")
        if target_hugepages is None:
            target_hugepages = self.get_target_hugepages()
        else:
            target_hugepages = int(target_hugepages)

        self.target_hugepages = target_hugepages

    @error_context.context_aware
    def check_hugepage_support(self):
        """
        Check whether the host support hugepage.
        Need check the cpu flag and kernel CML.
        """
        error_context.context("Check whether the host support hugepage.")
        host_cpu_flags = utils_misc.get_cpu_flags()
        host_ker_cml = utils_misc.get_ker_cmd()
        if self.hugepage_cpu_flag not in host_cpu_flags:
            raise exceptions.TestSkipError("Your host does not support hugepage,"
                                           "as miss the cpu flag %s on your host."
                                           "Please check cpu flags %s on the host" %
                                           (self.hugepage_cpu_flag, host_cpu_flags))
        if self.hugepage_match_str not in host_ker_cml:
            raise exceptions.TestSkipError("Your host does not support hugepage, "
                                           "as miss the %s in host kernel cmdline."
                                           "Please check kernel cmdline %s on host" %
                                           (self.hugepage_match_str, host_ker_cml))

    @error_context.context_aware
    def check_hugepage_size_as_expected(self):
        """
        Check whether the hugepage size as expected
        """
        error_context.context("Check whether the hugepage size as expected")
        if self.hugepage_size != self.expected_hugepage_size:
            raise exceptions.TestSkipError("The current hugepage size on host does "
                                           "not match the expected hugepage size.\n")

    def get_hugepage_size(self):
        """
        Get the current system setting for huge memory page size.
        """
        meminfo = open('/proc/meminfo', 'r').readlines()
        huge_line_list = [h for h in meminfo if h.startswith("Hugepagesize")]
        try:
            return int(huge_line_list[0].split()[1])
        except ValueError, e:
            raise ValueError("Could not get huge page size setting from "
                             "/proc/meminfo: %s" % e)

    def get_target_hugepages(self):
        """
        Calculate the target number of hugepages for testing purposes.
        """
        if self.vms < self.max_vms:
            self.vms = self.max_vms
        # memory of all VMs plus qemu overhead of 128MB per guest
        # (this value can be overridden in your cartesian config)
        vmsm = self.vms * (self.mem + self.qemu_overhead)
        target_hugepages = int(vmsm * 1024 / self.hugepage_size)

        # FIXME Now the buddyinfo can not get chunk info which is bigger
        # than 4M. So this will only fit for 2M size hugepages. Can not work
        # when hugepage size is 1G.
        # And sometimes huge page can not get all pages so decrease the page
        # for about 10 huge page to make sure the allocate can success

        decreased_pages = 10
        if self.hugepage_size > 2048:
            self.hugepage_force_allocate = "yes"

        if self.hugepage_force_allocate == "no":
            hugepage_allocated = open(self.kernel_hp_file, "r")
            available_hugepages = int(hugepage_allocated.read().strip())
            hugepage_allocated.close()
            chunk_bottom = int(math.log(self.hugepage_size / utils_memory.getpagesize(), 2))
            if ARCH == 'ppc64le':
                chunk_info = utils_memory.get_buddy_info(">=%s" % chunk_bottom)
            else:
                chunk_info = utils_memory.get_buddy_info(">=%s" % chunk_bottom,
                                                         zones="DMA32 Normal")
            for size in chunk_info:
                available_hugepages += int(chunk_info[size] * math.pow(2,
                                                                       int(int(size) - chunk_bottom)))

            available_hugepages = available_hugepages - decreased_pages
            if target_hugepages > available_hugepages:
                logging.warn("This test requires more huge pages than we"
                             " currently have, we'll try to allocate the"
                             " biggest number the system can support.")
                target_hugepages = available_hugepages
                available_mem = available_hugepages * self.hugepage_size
                self.suggest_mem = int(available_mem / self.vms / 1024 -
                                       self.qemu_overhead)
                if self.suggest_mem < self.lowest_mem_per_vm:
                    raise MemoryError("This host doesn't have enough free "
                                      "large memory pages for this test to "
                                      "run (only %s MB memory available for "
                                      "each guest)" % self.suggest_mem)

        return target_hugepages

    def get_multi_supported_hugepage_size(self):
        """
        As '/proc/meminfo' only show default huge page size, this function is
        for get huge page size of multiple huge page pools.

        For each huge page size supported by the running kernel, a
        subdirectory will exist, of the form:

            hugepages-${size}kB

        under /sys/kernel/mm/hugepages, get the support size and return a list.

        :return: supported size list in kB unit
        """
        hugepage_size = []
        if os.path.isdir(self.pool_path):
            for path_name in os.listdir(self.pool_path):
                logging.debug("path name is %s" % path_name)
                if os.path.isdir("%s/%s" % (self.pool_path, path_name)):
                    hugepage_size.append(path_name.split('-')[1][:-2])
                    logging.debug(path_name.split('-')[1][:-2])
            return hugepage_size
        else:
            raise ValueError("Root hugepage control sysfs directory %s did not"
                             " exist" % self.pool_path)

    def get_node_num_huge_pages(self, node, pagesize):
        """
        Get number of pages of certain page size under given numa node.

        :param node: string or int, node number
        :param pagesize: string or int, page size in kB
        :return: int, node huge pages number of given page size
        """
        node_page_path = "%s/node%s" % (self.sys_node_path, node)
        node_page_path += "/hugepages/hugepages-%skB/nr_hugepages" % pagesize
        if not os.path.isfile(node_page_path):
            raise ValueError("%s page size nr_hugepages file of node %s did "
                             "not exist" % (pagesize, node))
        out = process.system_output("cat %s" % node_page_path)
        return int(out)

    def set_node_num_huge_pages(self, num, node, pagesize):
        """
        Set number of pages of certain page size under given numa node.

        :param num: string or int, number of pages
        :param node: string or int, node number
        :param pagesize: string or int, page size in kB
        """
        node_page_path = "%s/node%s" % (self.sys_node_path, node)
        node_page_path += "/hugepages/hugepages-%skB/nr_hugepages" % pagesize
        if not os.path.isfile(node_page_path):
            raise ValueError("%s page size nr_hugepages file of node %s did "
                             "not exist" % (pagesize, node))
        process.system("echo %s > %s" % (num, node_page_path), shell=True)

    @error_context.context_aware
    def set_hugepages(self):
        """
        Sets the hugepage limit to the target hugepage value calculated.
        """
        error_context.context(
            "setting hugepages limit to %s" % self.target_hugepages)
        try:
            hugepage_cfg = open(self.kernel_hp_file, "r+")
            hp = hugepage_cfg.readline().strip()
            hugepage_cfg.close()
        except IOError:
            raise exceptions.TestSetupFail("Can't read kernel hugepage file")
        while int(hp) < self.target_hugepages:
            loop_hp = hp
            try:
                hugepage_cfg = open(self.kernel_hp_file, "r+")
                hugepage_cfg.write(str(self.target_hugepages))
                hugepage_cfg.flush()
                hugepage_cfg.seek(0)
                hp = int(hugepage_cfg.readline().strip())
                hugepage_cfg.close()
            except IOError:
                msg = "Can't read/write from kernel hugepage file"
                raise exceptions.TestSetupFail(msg)
            if loop_hp == hp:
                raise ValueError("Cannot set the kernel hugepage setting "
                                 "to the target value of %d hugepages." %
                                 self.target_hugepages)
        logging.debug("Successfully set %s large memory pages on host ",
                      self.target_hugepages)

    @error_context.context_aware
    def mount_hugepage_fs(self):
        """
        Verify if there's a hugetlbfs mount set. If there's none, will set up
        a hugetlbfs mount using the class attribute that defines the mount
        point.
        """
        error_context.context("mounting hugepages path")
        if not os.path.ismount(self.hugepage_path):
            if not os.path.isdir(self.hugepage_path):
                os.makedirs(self.hugepage_path)
            cmd = "mount -t hugetlbfs -o pagesize=%sK " % self.hugepage_size
            cmd += "none %s" % self.hugepage_path
            process.system(cmd)

    def setup(self):
        logging.debug("Number of VMs this test will use: %d", self.vms)
        logging.debug("Amount of memory used by each vm: %s", self.mem)
        logging.debug("System setting for large memory page size: %s",
                      self.hugepage_size)
        logging.debug("Number of large memory pages needed for this test: %s",
                      self.target_hugepages)
        self.set_hugepages()
        self.mount_hugepage_fs()

        return self.suggest_mem

    @error_context.context_aware
    def cleanup(self):
        if self.deallocate:
            error_context.context("trying to deallocate hugepage memory")
            try:
                process.system("umount %s" % self.hugepage_path)
            except process.CmdError:
                return
            process.system("echo 0 > %s" % self.kernel_hp_file, shell=True)
            logging.debug("Hugepage memory successfully deallocated")


class KSMConfig(object):

    def __init__(self, params, env):
        """
        :param params: Dict like object containing parameters for the test.
        """
        self.pages_to_scan = params.get("ksm_pages_to_scan")
        self.sleep_ms = params.get("ksm_sleep_ms")
        self.run = params.get("ksm_run", "1")
        self.ksm_module = params.get("ksm_module")

        if self.run == "yes":
            self.run = "1"
        elif self.run == "no":
            self.run == "0"

        # Get KSM module status if there is one
        self.ksmctler = utils_misc.KSMController()
        self.ksm_module_loaded = self.ksmctler.is_module_loaded()

        # load the ksm module for further information check
        if self.ksm_module and not self.ksm_module_loaded:
            self.ksmctler.load_ksm_module()

        # For ksmctl both pages_to_scan and sleep_ms should have value
        # So give some default value when it is not set up in params
        if self.pages_to_scan is None:
            self.pages_to_scan = "5000"
        if self.sleep_ms is None:
            self.sleep_ms = "50"

        # Check if ksmtuned is running before the test
        self.ksmtuned_process = self.ksmctler.get_ksmtuned_pid()

        # As ksmtuned may update KSM config most of the time we should disable
        # it when we test KSM
        self.disable_ksmtuned = params.get("disable_ksmtuned", "yes") == "yes"

        self.default_status = []
        self.default_status.append(self.ksmctler.get_ksm_feature("run"))
        self.default_status.append(self.ksmctler.get_ksm_feature(
            "pages_to_scan"))
        self.default_status.append(self.ksmctler.get_ksm_feature(
            "sleep_millisecs"))
        self.default_status.append(int(self.ksmtuned_process))
        self.default_status.append(self.ksm_module_loaded)

    def setup(self, env):
        if self.disable_ksmtuned:
            self.ksmctler.stop_ksmtuned()

        env.data["KSM_default_config"] = self.default_status
        self.ksmctler.set_ksm_feature({"run": self.run,
                                       "pages_to_scan": self.pages_to_scan,
                                       "sleep_millisecs": self.sleep_ms})

    def cleanup(self, env):
        default_status = env.data.get("KSM_default_config")

        # Get original ksm loaded status
        default_ksm_loaded = default_status.pop()
        if self.ksm_module and not default_ksm_loaded:
            self.ksmctler.unload_ksm_module()
            return

        # Remove pid of ksmtuned
        ksmtuned_pid = default_status.pop()
        if ksmtuned_pid != 0:
            # ksmtuned used to run in host. Start the process
            # and don't need set up the configures.
            self.ksmctler.start_ksmtuned()
            return

        if default_status == self.default_status:
            # Nothing changed
            return

        self.ksmctler.set_ksm_feature({"run": default_status[0],
                                       "pages_to_scan": default_status[1],
                                       "sleep_millisecs": default_status[2]})


class PrivateBridgeError(Exception):

    def __init__(self, brname):
        self.brname = brname

    def __str__(self):
        return "Bridge %s not available after setup" % self.brname


class PrivateBridgeConfig(object):
    __shared_state = {}

    def __init__(self, params=None):
        self.__dict__ = self.__shared_state
        if params is not None:
            self.brname = params.get("priv_brname", 'atbr0')
            self.subnet = params.get("priv_subnet", '192.168.58')
            self.ip_version = params.get("bridge_ip_version", "ipv4")
            self.dhcp_server_pid = None
            ports = params.get("priv_bridge_ports", '53 67').split()
            s_port = params.get("guest_port_remote_shell", "10022")
            if s_port not in ports:
                ports.append(s_port)
            ft_port = params.get("guest_port_file_transfer", "10023")
            if ft_port not in ports:
                ports.append(ft_port)
            u_port = params.get("guest_port_unattended_install", "13323")
            if u_port not in ports:
                ports.append(u_port)
            self.iptables_rules = self._assemble_iptables_rules(ports)
            self.physical_nic = params.get("physical_nic")
            self.force_create = False
            if params.get("bridge_force_create", "no") == "yes":
                self.force_create = True

    def _assemble_iptables_rules(self, port_list):
        rules = []
        index = 0
        for port in port_list:
            index += 1
            rules.append("INPUT %s -i %s -p tcp --dport %s -j ACCEPT" %
                         (index, self.brname, port))
            index += 1
            rules.append("INPUT %s -i %s -p udp --dport %s -j ACCEPT" %
                         (index, self.brname, port))
        rules.append("FORWARD 1 -m physdev --physdev-is-bridged -j ACCEPT")
        rules.append("FORWARD 2 -d %s.0/24 -o %s -m state "
                     "--state RELATED,ESTABLISHED -j ACCEPT" %
                     (self.subnet, self.brname))
        rules.append("FORWARD 3 -s %s.0/24 -i %s -j ACCEPT" %
                     (self.subnet, self.brname))
        rules.append("FORWARD 4 -i %s -o %s -j ACCEPT" %
                     (self.brname, self.brname))
        return rules

    def _add_bridge(self):
        process.system("brctl addbr %s" % self.brname)
        ip_fwd_path = "/proc/sys/net/%s/ip_forward" % self.ip_version
        ip_fwd = open(ip_fwd_path, "w")
        ip_fwd.write("1\n")
        process.system("brctl stp %s on" % self.brname)
        process.system("brctl setfd %s 4" % self.brname)
        if self.physical_nic:
            process.system("brctl addif %s %s" % (self.brname,
                                                  self.physical_nic))

    def _bring_bridge_up(self):
        process.system("ifconfig %s %s.1 up" % (self.brname, self.subnet))

    def _iptables_add(self, cmd):
        return process.system("iptables -I %s" % cmd)

    def _iptables_del(self, cmd):
        return process.system("iptables -D %s" % cmd)

    def _enable_nat(self):
        for rule in self.iptables_rules:
            self._iptables_add(rule)

    def _start_dhcp_server(self):
        process.system("service dnsmasq stop")
        process.system("dnsmasq --strict-order --bind-interfaces "
                       "--listen-address %s.1 --dhcp-range %s.2,%s.254 "
                       "--dhcp-lease-max=253 "
                       "--dhcp-no-override "
                       "--pid-file=%s/dnsmasq.pid "
                       "--log-facility=%s/dnsmasq.log" %
                       (self.subnet, self.subnet, self.subnet,
                        data_dir.get_tmp_dir(), data_dir.get_tmp_dir()))
        self.dhcp_server_pid = None
        try:
            self.dhcp_server_pid = int(open('%s/dnsmasq.pid' %
                                            data_dir.get_tmp_dir(), 'r').read())
        except ValueError:
            raise PrivateBridgeError(self.brname)
        logging.debug("Started internal DHCP server with PID %s",
                      self.dhcp_server_pid)

    def _verify_bridge(self):
        brctl_output = process.system_output("brctl show")
        if self.brname not in brctl_output:
            raise PrivateBridgeError(self.brname)

    def _get_bridge_info(self):
        return process.system_output("brctl show")

    def _br_exist(self):
        return self.brname in self._get_bridge_info()

    def _br_in_use(self):
        output = self._get_bridge_info()
        for line in output.split("\n"):
            if line.startswith(self.brname):
                # len == 4 means there is a TAP using the bridge
                # so don't try to clean it up
                if len(line.split()) < 4:
                    return False
        return True

    def setup(self):
        if self._br_exist() and self.force_create:
            self._bring_bridge_down()
            self._remove_bridge()
        if not self._br_exist():
            logging.info("Configuring KVM test private bridge %s", self.brname)
            try:
                self._add_bridge()
            except Exception:
                self._remove_bridge()
                raise
            try:
                self._bring_bridge_up()
            except Exception:
                self._bring_bridge_down()
                self._remove_bridge()
                raise
            try:
                self._enable_nat()
            except Exception:
                self._disable_nat()
                self._bring_bridge_down()
                self._remove_bridge()
                raise
            try:
                self._start_dhcp_server()
            except Exception:
                self._stop_dhcp_server()
                self._disable_nat()
                self._bring_bridge_down()
                self._remove_bridge()
                raise
            # Fix me the physical_nic always down after setup
            # Need manually up.
            if self.physical_nic:
                time.sleep(5)
                process.system("ifconfig %s up" % self.physical_nic)

            self._verify_bridge()

    def _stop_dhcp_server(self):
        if self.dhcp_server_pid is not None:
            try:
                os.kill(self.dhcp_server_pid, 15)
            except OSError:
                pass
        else:
            try:
                dhcp_server_pid = int(open('%s/dnsmasq.pid' %
                                           data_dir.get_tmp_dir(), 'r').read())
            except ValueError:
                return
            try:
                os.kill(dhcp_server_pid, 15)
            except OSError:
                pass

    def _bring_bridge_down(self):
        process.system("ifconfig %s down" % self.brname, ignore_status=True)

    def _disable_nat(self):
        for rule in self.iptables_rules:
            split_list = rule.split(' ')
            # We need to remove numbering here
            split_list.pop(1)
            rule = " ".join(split_list)
            self._iptables_del(rule)

    def _remove_bridge(self):
        process.system("brctl delbr %s" % self.brname, ignore_status=True)

    def cleanup(self):
        if not self._br_in_use():
            logging.debug(
                "Cleaning up KVM test private bridge %s", self.brname)
            self._stop_dhcp_server()
            self._disable_nat()
            self._bring_bridge_down()
            self._remove_bridge()


class PrivateOvsBridgeConfig(PrivateBridgeConfig):

    def __init__(self, params=None):
        super(PrivateOvsBridgeConfig, self).__init__(params)
        ovs = versionable_class.factory(openvswitch.OpenVSwitchSystem)()
        ovs.init_system()
        self.ovs = ovs

    def _get_bridge_info(self):
        return self.ovs.status()

    def _br_exist(self):
        return "Bridge \"%s\"" % self.brname in self._get_bridge_info()

    def _br_in_use(self):
        output = self._get_bridge_info()
        for br_info in output.split("Bridge"):
            br_info = br_info.strip()
            if (br_info and re.match(self.brname, br_info) and
                    len(re.findall("Port\s+", br_info)) == 1):
                return False
        return True

    def _verify_bridge(self):
        self.ovs.check()

    def _add_bridge(self):
        self.ovs.add_br(self.brname)

    def _remove_bridge(self):
        self.ovs.del_br(self.brname)


class PciAssignable(object):

    """
    Request PCI assignable devices on host. It will check whether to request
    PF (physical Functions) or VF (Virtual Functions).
    """

    def __init__(self, driver=None, driver_option=None, host_set_flag=None,
                 kvm_params=None, vf_filter_re=None, pf_filter_re=None,
                 device_driver=None, nic_name_re=None, static_ip=None,
                 net_mask=None, start_addr_PF=None, pa_type=None):
        """
        Initialize parameter 'type' which could be:
        vf: Virtual Functions
        pf: Physical Function (actual hardware)
        mixed:  Both includes VFs and PFs

        If pass through Physical NIC cards, we need to specify which devices
        to be assigned, e.g. 'eth1 eth2'.

        If pass through Virtual Functions, we need to specify max vfs in driver
        e.g. max_vfs = 7 in config file.

        :param type: PCI device type.
        :type type: string
        :param driver: Kernel module for the PCI assignable device.
        :type driver: string
        :param driver_option: Module option to specify the maximum number of
                VFs (eg 'max_vfs=7')
        :type driver_option: string
        :param host_set_flag: Flag for if the test should setup host env:
               0: do nothing
               1: do setup env
               2: do cleanup env
               3: setup and cleanup env
        :type host_set_flag: string
        :param kvm_params: a dict for kvm module parameters default value
        :type kvm_params: dict
        :param vf_filter_re: Regex used to filter vf from lspci.
        :type vf_filter_re: string
        :param pf_filter_re: Regex used to filter pf from lspci.
        :type pf_filter_re: string
        :param static_ip: Flag to be set if the test should assign static IP
        :param start_addr_PF: Starting private IPv4 address for the PF interface
        :param pa_type: pci_assignable type, pf or vf
        """
        self.devices = []
        self.driver = driver
        self.driver_option = driver_option
        self.name_list = []
        self.devices_requested = 0
        self.pf_vf_info = []
        self.dev_unbind_drivers = {}
        self.dev_drivers = {}
        self.vf_filter_re = vf_filter_re
        self.pf_filter_re = pf_filter_re
        self.static_ip = static_ip
        self.net_mask = net_mask
        self.start_addr_PF = start_addr_PF
        self.pa_type = pa_type

        if nic_name_re:
            self.nic_name_re = nic_name_re
        else:
            self.nic_name_re = "\w+(?=: flags)|eth[0-9](?=\s*Link)"
        if device_driver:
            if device_driver == "pci-assign":
                self.device_driver = "pci-stub"
            else:
                self.device_driver = device_driver
        else:
            self.device_driver = "pci-stub"
        if host_set_flag is not None:
            self.setup = int(host_set_flag) & 1 == 1
            self.cleanup = int(host_set_flag) & 2 == 2
        else:
            self.setup = False
            self.cleanup = False
        self.kvm_params = kvm_params
        self.auai_path = None
        if self.kvm_params is not None:
            for i in self.kvm_params:
                if "allow_unsafe_assigned_interrupts" in i:
                    self.auai_path = i
        if self.setup:
            if not self.sr_iov_setup():
                msg = "SR-IOV setup on host failed"
                raise exceptions.TestSetupFail(msg)

    def add_device(self, device_type="vf", name=None, mac=None):
        """
        Add device type and name to class.

        :param device_type: vf/pf device is added.
        :type device_type: string
        :param name: Physical device interface name. eth1 or others
        :type name: string
        :param mac: set mac address for vf.
        :type mac: string
        """
        device = {}
        device['type'] = device_type
        if name is not None:
            device['name'] = name
        if mac:
            device['mac'] = mac
        self.devices.append(device)
        self.devices_requested += 1

    def _get_pf_pci_id(self, name=None):
        """
        Get the PF PCI ID according to name.
        It returns the first free pf, if no name matched.

        :param name: Name of the PCI device.
        :type name: string
        :return: pci id of the PF device.
        :rtype: string
        """
        pf_id = None
        if self.pf_vf_info:
            for pf in self.pf_vf_info:
                if name and "ethname" in pf and name == pf["ethname"]:
                    pf["occupied"] = True
                    pf_id = pf["pf_id"]
                    break
            if pf_id is None:
                for pf in self.pf_vf_info:
                    if not pf["occupied"]:
                        pf["occupied"] = True
                        pf_id = pf["pf_id"]
                        break
        return pf_id

    def _get_vf_pci_id(self, name=None):
        """
        Get the VF PCI ID according to name.
        It returns the first free vf, if no name matched.

        :param name: Name of the PCI device.
        :type name: string
        :return: pci id of the VF device.
        :rtype: string
        """
        vf_id = None
        if self.pf_vf_info:
            for pf in self.pf_vf_info:
                if name and "ethname" in pf and name == pf["ethname"]:
                    for vf in pf["vf_ids"]:
                        vf_id = vf["vf_id"]
                        if (not vf["occupied"] and
                                not self.is_binded_to_stub(vf_id)):
                            vf["occupied"] = True
                            return vf_id
            if vf_id is None:
                for pf in self.pf_vf_info:
                    if pf["occupied"]:
                        continue
                    for vf in pf["vf_ids"]:
                        vf_id = vf["vf_id"]
                        if (not vf["occupied"] and
                                not self.is_binded_to_stub(vf_id)):
                            vf["occupied"] = True
                            return vf_id

    @error_context.context_aware
    def _release_dev(self, pci_id):
        """
        Release a single PCI device.

        :param pci_id: PCI ID of a given PCI device.
        :type pci_id: string
        :return: True if successfully release the device. else false.
        :rtype: bool
        """
        base_dir = "/sys/bus/pci"
        drv_path = os.path.join(base_dir, "devices/%s/driver" % pci_id)
        if self.device_driver in os.readlink(drv_path):
            error_context.context(
                "Release device %s to host" % pci_id, logging.info)

            stub_path = os.path.join(base_dir,
                                     "drivers/%s" % self.device_driver)
            cmd = "echo '%s' > %s/unbind" % (pci_id, stub_path)
            logging.info("Run command in host: %s" % cmd)
            try:
                output = None
                output = process.system_output(cmd, shell=True, timeout=60)
            except Exception:
                msg = "Command %s fail with output %s" % (cmd, output)
                logging.error(msg)
                return False

            drivers_probe = os.path.join(base_dir, "drivers_probe")
            cmd = "echo '%s' > %s" % (pci_id, drivers_probe)
            logging.info("Run command in host: %s" % cmd)
            try:
                output = None
                output = process.system_output(cmd, shell=True, timeout=60)
            except Exception:
                msg = "Command %s fail with output %s" % (cmd, output)
                logging.error(msg)
                return False
        if self.is_binded_to_stub(pci_id):
            return False
        else:
            for pf in self.pf_vf_info:
                for vf in pf["vf_ids"]:
                    if vf["vf_id"] == pci_id:
                        vf["occupied"] = False
        return True

    def get_vf_status(self, vf_id):
        """
        Check whether one vf is assigned to VM.

        :param vf_id: vf id to check.
        :type vf_id: string
        :return: Return True if vf has already assigned to VM. Else
                 return false.
        :rtype: bool
        """
        base_dir = "/sys/bus/pci"
        tub_path = os.path.join(base_dir, "drivers/pci-stub")
        vf_res_path = os.path.join(tub_path, "%s/resource*" % vf_id)
        cmd = "lsof %s" % vf_res_path
        output = process.system_output(cmd, timeout=60, ignore_status=True)
        if 'qemu' in output:
            return True
        else:
            return False

    def get_vf_num_by_id(self, vf_id):
        """
        Return corresponding pf eth name and vf num according to vf id.

        :param vf_id: vf id to check.
        :type vf_id: string
        :return: PF device name and vf num.
        :rtype: string
        """
        for pf_info in self.pf_vf_info:
            for vf_info in pf_info.get('vf_ids'):
                if vf_id == vf_info["vf_id"]:
                    return pf_info['ethname'], pf_info["vf_ids"].index(vf_info)
        raise ValueError("Could not find vf id '%s' in '%s'" % (vf_id,
                                                                self.pf_vf_info))

    def get_pf_vf_info(self):
        """
        Get pf and vf related information in this host that match ``self.pf_filter_re``.

        for every pf it will create following information:

        pf_id:
            The id of the pf device.
        occupied:
            Whether the pf device assigned or not
        vf_ids:
            Id list of related vf in this pf.
        ethname:
            eth device name in host for this pf.

        :return: return a list contains pf vf information.
        :rtype: builtin.list
        """

        pf_ids = self.get_pf_ids()
        pf_vf_dict = []
        for pf_id in pf_ids:
            pf_info = {}
            vf_ids = []
            full_id = utils_misc.get_full_pci_id(pf_id)
            pf_info["pf_id"] = full_id
            pf_info["occupied"] = False
            d_link = os.path.join("/sys/bus/pci/devices", full_id)
            txt = process.system_output("ls %s" % d_link)
            re_vfn = "(virtfn\d+)"
            paths = re.findall(re_vfn, txt)
            for path in paths:
                f_path = os.path.join(d_link, path)
                vf_id = os.path.basename(os.path.realpath(f_path))
                vf_info = {}
                vf_info["vf_id"] = vf_id
                vf_info["occupied"] = False
                vf_ids.append(vf_info)
            pf_info["vf_ids"] = vf_ids
            pf_vf_dict.append(pf_info)
        if_out = process.system_output("ifconfig -a")
        ethnames = re.findall(self.nic_name_re, if_out)
        for eth in ethnames:
            cmd = "ethtool -i %s | awk '/bus-info/ {print $2}'" % eth
            pci_id = process.system_output(cmd, shell=True).strip()
            if not pci_id:
                continue
            for pf in pf_vf_dict:
                if pci_id in pf["pf_id"]:
                    pf["ethname"] = eth
        return pf_vf_dict

    def get_vf_devs(self, devices=None):
        """
        Get all unused VFs PCI IDs.

        :param devices: List of device dict that contain PF VF information.
        :type devices: List of dict
        :return: List of all available PCI IDs for Virtual Functions.
        :rtype: List of string
        """
        vf_ids = []
        if not devices:
            devices = self.devices
        logging.info("devices = %s", devices)
        for device in devices:
            if device['type'] == 'vf':
                name = device.get('name', None)
                vf_id = self._get_vf_pci_id(name)
                logging.info("vf_id = %s", vf_id)
                if not vf_id:
                    continue
                vf_ids.append(vf_id)
        return vf_ids

    def get_pf_devs(self, devices=None):
        """
        Get PFs PCI IDs requested by self.devices.
        It will try to get PF by device name.
        It will still return it, if device name you set already occupied.
        Please set unoccupied device name. If not sure, please just do not
        set device name. It will return unused PF list.

        :param devices: List of device dict that contain PF VF information.
        :type devices: List of dict
        :return: List with all PCI IDs for the physical hardware requested
        :rtype: List of string
        """
        pf_ids = []
        if not devices:
            devices = self.devices
        for device in devices:
            if device['type'] == 'pf':
                name = device.get('name', None)
                pf_id = self._get_pf_pci_id(name)
                if not pf_id:
                    continue
                pf_ids.append(pf_id)
        return pf_ids

    def get_devs(self, devices=None):
        """
        Get devices' PCI IDs according to parameters set in self.devices.

        :param devices: List of device dict that contain PF VF information.
        :type devices: List of dict
        :return: List of all available devices' PCI IDs
        :rtype: List of string
        """
        base_dir = "/sys/bus/pci"
        if not devices:
            devices = self.devices
        if isinstance(devices, dict):
            devices = [devices]
        pf_ids = self.get_pf_devs(devices)
        logging.info("pf_ids = %s", pf_ids)
        vf_ids = self.get_vf_devs(devices)
        logging.info("vf_ids = %s", vf_ids)
        vf_ids.sort()
        dev_ids = []

        for device in devices:
            d_type = device.get("type", "vf")
            if d_type == "vf":
                dev_id = vf_ids.pop(0)
                (ethname, vf_num) = self.get_vf_num_by_id(dev_id)
                set_mac_cmd = "ip link set dev %s vf %s mac %s " % (ethname,
                                                                    vf_num,
                                                                    device["mac"])
                process.run(set_mac_cmd)

            elif d_type == "pf":
                dev_id = pf_ids.pop(0)
            dev_ids.append(dev_id)
            unbind_driver = os.path.realpath(os.path.join(base_dir,
                                                          "devices/%s/driver" % dev_id))
            self.dev_unbind_drivers[dev_id] = unbind_driver
        if len(dev_ids) != len(devices):
            logging.error("Did not get enough PCI Device")
        return dev_ids

    def get_vfs_count(self):
        """
        Get VFs count number according to lspci.
        """
        # FIXME: Need to think out a method of identify which
        # 'virtual function' belongs to which physical card considering
        # that if the host has more than one 82576 card. PCI_ID?
        cmd = "lspci | grep '%s' | wc -l" % self.vf_filter_re
        vf_num = int(process.system_output(cmd, shell=True, verbose=False))
        logging.info("Found %s vf in host", vf_num)
        return vf_num

    def get_same_group_devs(self, pci_id):
        """
        Get the device that in same iommu group.

        :param pci_id: Device's pci_id
        :type pci_id: string
        :return: Return the device's pci id that in same group with pci_id.
        :rtype: List of string.
        """
        pci_ids = []
        base_dir = "/sys/bus/pci/devices"
        devices_link = os.path.join(base_dir,
                                    "%s/iommu_group/devices/" % pci_id)
        out = process.system_output("ls %s" % devices_link)

        if out:
            pci_ids = out.split()
        return pci_ids

    def get_pf_ids(self):
        """
        Get the id of PF devices
        """
        cmd = "lspci | grep -v 'Virtual Function' |awk '/%s/ {print $1}'" % self.pf_filter_re
        PF_devices = [i for i in process.system_output(
                cmd, shell=True).splitlines()]
        if not PF_devices:
            raise exceptions.TestSkipError("No specified pf found in the host!")
        pf_ids = []
        for pf in PF_devices:
            pf_id = utils_misc.get_full_pci_id(pf)
            pf_ids.append(pf_id)
        return pf_ids

    def assign_static_ip(self):
        """
        Set the static IP for the PF devices for mlx5_core driver
        """
        # This function assigns static IP for the PF devices
        pf_devices = self.get_pf_ids()
        if (not self.start_addr_PF) or (not self.net_mask):
            raise exceptions.TestSetupFail(
                "No IP / netmask found, please populate starting IP address for PF devices in configuration file")
        ip_addr = netaddr.IPAddress(self.start_addr_PF)
        for PF in pf_devices:
            ifname = utils_misc.get_interface_from_pci_id(PF)
            ip_assign = "ifconfig %s %s netmask %s up" % (
                ifname, ip_addr, self.net_mask)
            logging.info("assign IP to PF device %s : %s", PF,
                         ip_assign)
            cmd = process.system(ip_assign, shell=True, ignore_status=True)
            if cmd:
                raise exceptions.TestSetupFail("Failed to assign IP : %s"
                                               % cmd)
            ip_addr += 1
        return True

    def check_vfs_count(self):
        """
        Check VFs count number according to the parameter driver_options.
        """
        # The VF count should be multiplied with the total no.of PF's
        # present, rather than fixed number of network interfaces.
        expected_count = int((re.findall("(\d+)", self.driver_option)[0])) * len(self.get_pf_ids())
        return (self.get_vfs_count() == expected_count)

    def is_binded_to_stub(self, full_id):
        """
        Verify whether the device with full_id is already binded to driver.

        :param full_id: Full ID for the given PCI device
        :type full_id: String
        """
        base_dir = "/sys/bus/pci/"
        stub_path = os.path.join(base_dir, "drivers/%s" % self.device_driver)
        return os.path.exists(os.path.join(stub_path, full_id))

    def set_vf(self, pci_pf, vf_no="0"):
        """
        For ppc64le we have to echo no of VFs to be enabled

        :params pci_pf: Pci id to be virtualized with VFs
        :params vf_no: Number of Vfs to be virtualized
        :return: True on success, False on failure
        """
        cmd = "echo %s > /sys/bus/pci/devices/%s/sriov_numvfs" % (vf_no, pci_pf)
        if process.system(cmd, shell=True, ignore_status=True):
            logging.debug("Failed to set %s vfs in %s", vf_no, pci_pf)
            return False
        # When the VFs loaded on a PF are > 10 [I have tested till 63 which is
        # max VF supported by Mellanox CX4 cards],VFs probe on host takes bit
        # more time than usual.
        # Without this sleep ifconfig would fail With below error, since not all
        # probed interfaces of VF would have got properly renamed:
        # CmdError: Command 'ifconfig -a' failed (rc=1)
        if int(vf_no) > 10:
            time.sleep(60)
        return True

    def remove_driver(self, driver=None):
        """
        Method to remove driver

        :param driver: driver name
        :return: True on success, False on failure
        """
        if not driver:
            driver = self.driver
        if driver and driver != "mlx5_core":
            cmd = "modprobe -r %s" % driver
        if ARCH == 'ppc64le' and driver == 'mlx5_core':
            pf_devices = self.get_pf_ids()
            logging.info("Mellanox PF devices '%s'", pf_devices)
            for PF in pf_devices:
                if not self.set_vf(PF):
                    return False
            cmd = "rmmod mlx5_ib;modprobe -r mlx5_core;modprobe mlx5_ib"
        if process.system(cmd, ignore_status=True, shell=True):
            logging.debug("Failed to remove driver: %s", driver)
            return False
        return True

    @error_context.context_aware
    def modprobe_driver(self, driver=None):
        """
        Method to modprobe driver

        :param driver: driver name
        :return: True on success, False on failure
        """
        if not driver:
            driver = self.driver
        msg = "Loading the driver '%s'" % driver
        error_context.context(msg, logging.info)
        cmd = "modprobe %s" % driver
        if process.system(cmd, ignore_status=True, shell=True):
            logging.debug("Failed to modprobe driver: %s", driver)
            return False
        return True

    @error_context.context_aware
    def sr_iov_setup(self):
        """
        Ensure the PCI device is working in sr_iov mode.

        Check if the PCI hardware device drive is loaded with the appropriate,
        parameters (number of VFs), and if it's not, perform setup.

        :return: True, if the setup was completed successfully, False otherwise.
        :rtype: bool
        """
        # Check if the host support interrupt remapping. On PowerPC interrupt
        # remapping is not required
        error_context.context("Set up host env for PCI assign test",
                              logging.info)
        if ARCH != 'ppc64le':
            kvm_re_probe = True
            dmesg = process.system_output("dmesg", verbose=False)
            ecap = re.findall("ecap\s+(.\w+)", dmesg)
            if not ecap:
                logging.error("Fail to check host interrupt remapping support")
            else:
                if int(ecap[0], 16) & 8 == 8:
                    # host support interrupt remapping.
                    # No need enable allow_unsafe_assigned_interrupts.
                    kvm_re_probe = False
                if self.kvm_params is not None:
                    if self.auai_path and self.kvm_params[self.auai_path] == "Y":
                        kvm_re_probe = False
            # Try to re probe kvm module with interrupt remapping support
            if kvm_re_probe and self.auai_path:
                cmd = "echo Y > %s" % self.auai_path
                error_context.context("enable PCI passthrough with '%s'" % cmd,
                                      logging.info)
                try:
                    process.system(cmd)
                except Exception:
                    logging.debug(
                        "Can not enable the interrupt remapping support")
            lnk = "/sys/module/vfio_iommu_type1/parameters/allow_unsafe_interrupts"
            if self.device_driver == "vfio-pci":
                # If driver is not available modprobe it
                if process.system('lsmod | grep vfio_pci', ignore_status=True,
                                  shell=True):
                    self.modprobe_driver(driver="vfio-pci")
                    time.sleep(3)
                if not ecap or (int(ecap[0], 16) & 8 != 8):
                    cmd = "echo Y > %s" % lnk
                    error_context.context("enable PCI passthrough with '%s'" % cmd,
                                          logging.info)
                    process.run(cmd)
        else:
            if self.device_driver == "vfio-pci":
                if process.system('lsmod | grep vfio_pci', ignore_status=True,
                                  shell=True):
                    self.modprobe_driver(driver="vfio-pci")
                    time.sleep(3)
        re_probe = False
        # If driver not available after modprobe try to remove it and reprobe
        if process.system("lsmod | grep -w %s" % self.driver, ignore_status=True,
                          shell=True):
            re_probe = True
        # If driver is available and pa_type is vf then set VFs
        elif self.pa_type == 'vf':
            pf_devices = self.get_pf_ids()
            if (self.static_ip):
                self.assign_static_ip()
            for PF in pf_devices:
                if not self.set_vf(PF, self.driver_option):
                    re_probe = True
            if not self.check_vfs_count():
                re_probe = True
        if not re_probe:
            self.setup = None
            return True

        # Re-probe driver with proper number of VFs once more and raise
        # exception
        if re_probe:
            if not self.remove_driver() or not self.modprobe_driver():
                return False
            pf_devices = self.get_pf_ids()
            if (self.static_ip):
                self.assign_static_ip()
            if self.pa_type == 'vf':
                for PF in pf_devices:
                    if not self.set_vf(PF, self.driver_option):
                        return False
                if not self.check_vfs_count():
                    # Even after re-probe there are no VFs created
                    return False
            dmesg = process.system_output("dmesg", timeout=60,
                                          ignore_status=True,
                                          verbose=False)
            file_name = "host_dmesg_after_load_%s.txt" % self.driver
            logging.info("Log dmesg after loading '%s' to '%s'.", self.driver,
                         file_name)
            utils_misc.log_line(file_name, dmesg)
            self.setup = None
            return True

    def sr_iov_cleanup(self):
        """
        Clean up the sriov setup

        Check if the PCI hardware device drive is loaded with the appropriate,
        parameters (none of VFs), and if it's not, perform cleanup.

        :return: True, if the setup was completed successfully, False otherwise.
        :rtype: bool
        """
        # Check if the host support interrupt remapping. On PowerPC interrupt
        # remapping is not required
        error_context.context(
            "Clean up host env after PCI assign test", logging.info)
        if ARCH != 'ppc64le':
            if self.kvm_params is not None:
                for kvm_param, value in self.kvm_params.items():
                    if open(kvm_param, "r").read().strip() != value:
                        cmd = "echo %s > %s" % (value, kvm_param)
                        logging.info("Write '%s' to '%s'", value, kvm_param)
                        try:
                            process.system(cmd)
                        except Exception:
                            logging.error("Failed to write  '%s' to '%s'", value,
                                          kvm_param)

        re_probe = False
        # if lsmod lists the driver then remove it to clean up
        if not process.system('lsmod | grep %s' % self.driver,
                              ignore_status=True, shell=True):
            if not self.remove_driver():
                re_probe = True

        # if removing driver fails, give one more try to remove the driver and
        # raise exception
        if re_probe:
            if not self.remove_driver():
                return False
        return True

    @error_context.context_aware
    def request_devs(self, devices=None):
        """
        Implement setup process: unbind the PCI device and then bind it
        to the device driver.

        :param devices: List of device dict
        :type devices: List of dict
        :return: List of successfully requested devices' PCI IDs.
        :rtype: List of string
        """
        if not self.pf_vf_info:
            self.pf_vf_info = self.get_pf_vf_info()
        base_dir = "/sys/bus/pci"
        stub_path = os.path.join(base_dir, "drivers/%s" % self.device_driver)
        self.pci_ids = self.get_devs(devices)
        logging.info("The following pci_ids were found: %s", self.pci_ids)
        requested_pci_ids = []

        # Setup all devices specified for assignment to guest
        for p_id in self.pci_ids:
            if self.device_driver == "vfio-pci":
                pci_ids = self.get_same_group_devs(p_id)
                logging.info(
                    "Following devices are in same group: %s", pci_ids)
            else:
                pci_ids = [p_id]
            for pci_id in pci_ids:
                drv_path = os.path.join(base_dir, "devices/%s/driver" % pci_id)
                # In some cases, for example on ppc64le platform when using
                # Mellanox Connectx 4 or Mellanox Connectx-Pro SR-IOV enabled
                # cards, initially drv_path will not exist for the VF PCI device
                # and the test_setup will fail. Introducing below check to
                # handle such situations
                if not os.path.exists(drv_path):
                    dev_prev_driver = ""
                else:
                    dev_prev_driver = os.path.realpath(os.path.join(drv_path,
                                                                    os.readlink(drv_path)))
                self.dev_drivers[pci_id] = dev_prev_driver

                # Judge whether the device driver has been binded to stub
                if not self.is_binded_to_stub(pci_id):
                    error_context.context("Bind device %s to stub" % pci_id,
                                          logging.info)
                    # On Power architecture using short id would result in
                    # pci device lookup failure while writing vendor id to
                    # stub_new_id/stub_remove_id. Instead we should be using
                    # pci id as-is for vendor id.
                    if ARCH != 'ppc64le':
                        short_id = pci_id[5:]
                        vendor_id = utils_misc.get_vendor_from_pci_id(short_id)
                    else:
                        vendor_id = utils_misc.get_vendor_from_pci_id(pci_id)
                    stub_new_id = os.path.join(stub_path, 'new_id')
                    unbind_dev = os.path.join(drv_path, 'unbind')
                    stub_bind = os.path.join(stub_path, 'bind')
                    stub_remove_id = os.path.join(stub_path, 'remove_id')

                    info_write_to_files = [(vendor_id, stub_new_id),
                                           (pci_id, unbind_dev),
                                           (pci_id, stub_bind),
                                           (vendor_id, stub_remove_id)]

                    for content, f_name in info_write_to_files:
                        try:
                            logging.info("Write '%s' to file '%s'", content,
                                         f_name)
                            with open(f_name, 'w') as fn:
                                fn.write(content)
                        except IOError:
                            logging.debug("Failed to write %s to file %s",
                                          content, f_name)
                            continue

                    if not self.is_binded_to_stub(pci_id):
                        logging.error(
                            "Binding device %s to stub failed", pci_id)
                    continue
                else:
                    logging.debug("Device %s already binded to stub", pci_id)
            requested_pci_ids.append(p_id)
        return requested_pci_ids

    @error_context.context_aware
    def release_devs(self):
        """
        Release all PCI devices currently assigned to VMs back to the
        virtualization host.
        """
        try:
            for pci_id in self.dev_drivers:
                if not self._release_dev(pci_id):
                    logging.error(
                        "Failed to release device %s to host", pci_id)
                else:
                    logging.info("Released device %s successfully", pci_id)
            if self.cleanup:
                self.sr_iov_cleanup()
                self.devices = []
                self.devices_requested = 0
                self.dev_unbind_drivers = {}
        except Exception:
            return


class LibvirtPolkitConfig(object):

    """
    Enable polkit access driver for libvirtd and set polkit rules.

    For setting JavaScript polkit rule, using template of rule to satisfy
    libvirt ACL API testing need, just replace keys in template.

    Create a non-privileged user 'testacl' for test if given
    'unprivileged_user' contains 'EXAMPLE', and delete the user at cleanup.

    Multiple rules could be add into one config file while action_id string
    is offered space separated.

    e.g.
    action_id = "org.libvirt.api.domain.start org.libvirt.api.domain.write"

    then 2 actions "org.libvirt.api.domain.start" and
    "org.libvirt.api.domain.write" specified, which could be used to generate
    2 rules in one config file.
    """

    def __init__(self, params):
        """
        :param params: Dict like object containing parameters for the test.
        """
        self.libvirtd_path = "/etc/libvirt/libvirtd.conf"
        self.libvirtd_backup_path = "/etc/libvirt/libvirtd.conf.virttest.backup"
        self.polkit_rules_path = "/etc/polkit-1/rules.d/"
        self.polkit_rules_path += "500-libvirt-acl-virttest.rules"
        self.polkit_name = "polkit"
        distro_obj = distro.detect()
        # For ubuntu polkitd have to be used
        if distro_obj.name.lower().strip() == 'ubuntu':
            self.polkit_name = "polkitd"
        self.polkitd = service.Factory.create_service(self.polkit_name)

        if params.get("action_id"):
            self.action_id = params.get("action_id").split()
        else:
            self.action_id = []
        self.user = params.get("unprivileged_user")
        if params.get("action_lookup"):
            # The action_lookup string should be separated by space and
            # each separated string should have ':' which represent key:value
            # for later use.
            self.attr = params.get("action_lookup").split()
        else:
            self.attr = []

    def file_replace_append(self, fpath, pat, repl):
        """
        Replace pattern in file with replacement str if pattern found in file,
        else append the replacement str to file.

        :param fpath: string, the file path
        :param pat: string, the pattern string
        :param repl: string, the string to replace
        """
        try:
            lines = open(fpath).readlines()
            if not any(re.search(pat, line) for line in lines):
                f = open(fpath, 'a')
                f.write(repl + '\n')
                f.close()
                return
            else:
                out_fpath = fpath + ".tmp"
                out = open(out_fpath, "w")
                for line in lines:
                    if re.search(pat, line):
                        out.write(repl + '\n')
                    else:
                        out.write(line)
                out.close()
                os.rename(out_fpath, fpath)
        except Exception:
            raise PolkitWriteLibvirtdConfigError("Failed to update file '%s'."
                                                 % fpath)

    def _setup_libvirtd(self):
        """
        Config libvirtd
        """
        # Backup libvirtd.conf
        shutil.copy(self.libvirtd_path, self.libvirtd_backup_path)

        # Set the API access control scheme
        access_str = "access_drivers = [ \"polkit\" ]"
        access_pat = "^ *access_drivers"
        self.file_replace_append(self.libvirtd_path, access_pat, access_str)

        # Set UNIX socket access controls
        sock_rw_str = "unix_sock_rw_perms = \"0777\""
        sock_rw_pat = "^ *unix_sock_rw_perms"
        self.file_replace_append(self.libvirtd_path, sock_rw_pat, sock_rw_str)

        # Set authentication mechanism
        auth_unix_str = "auth_unix_rw = \"none\""
        auth_unix_pat = "^ *auth_unix_rw"
        self.file_replace_append(self.libvirtd_path, auth_unix_pat,
                                 auth_unix_str)

    def _set_polkit_conf(self):
        """
        Set polkit libvirt ACL rule config file
        """
        # polkit template string
        template = "polkit.addRule(function(action, subject) {\n"
        template += "RULE"
        template += "});"

        # polkit rule template string
        rule = "    if (action.id == 'ACTION_ID'"
        rule += " && subject.user == 'USERNAME') {\n"
        rule += "HANDLE"
        rule += "    }\n"

        handle = "        if (ACTION_LOOKUP) {\n"
        handle += "            return polkit.Result.YES;\n"
        handle += "        } else {\n"
        handle += "            return polkit.Result.NO;\n"
        handle += "        }\n"

        action_str = "action.lookup('ATTR') == 'VAL'"

        try:
            # replace keys except 'ACTION_ID', these keys will remain same
            # as in different rules
            rule_tmp = rule.replace('USERNAME', self.user)

            # replace HANDLE part in rule
            action_opt = []
            if self.attr:
                for i in range(len(self.attr)):
                    attr_tmp = self.attr[i].split(':')
                    action_tmp = action_str.replace('ATTR', attr_tmp[0])
                    action_tmp = action_tmp.replace('VAL', attr_tmp[1])
                    action_opt.append(action_tmp)
                    if i > 0:
                        action_opt[i] = " && " + action_opt[i]

                action_tmp = ""
                for i in range(len(action_opt)):
                    action_tmp += action_opt[i]

                # replace ACTION_LOOKUP with string from self.attr
                handle_tmp = handle.replace('ACTION_LOOKUP', action_tmp)
                rule_tmp = rule_tmp.replace('HANDLE', handle_tmp)
            else:
                rule_tmp = rule_tmp.replace('HANDLE', "    ")

            # replace 'ACTION_ID' in loop and generate rules
            rules = ""
            for i in range(len(self.action_id)):
                rules += rule_tmp.replace('ACTION_ID', self.action_id[i])

            # replace 'RULE' with rules in polkit template string
            self.template = template.replace('RULE', rules)
            logging.debug("The polkit config rule is:\n%s" % self.template)

            # write the config file
            genio.write_file(self.polkit_rules_path, self.template)
        except Exception, e:
            raise PolkitRulesSetupError("Set polkit rules file failed: %s", e)

    def setup(self):
        """
        Enable polkit libvirt access driver and setup polkit ACL rules.
        """
        self._setup_libvirtd()
        # Use 'testacl' if unprivileged_user in cfg contains string 'EXAMPLE',
        # and if user 'testacl' is not exist on host, create it for test.
        if self.user.count('EXAMPLE'):
            cmd = "id testacl"
            if process.system(cmd, ignore_status=True):
                logging.debug("Create new user 'testacl' on host.")
                cmd = "useradd testacl"
                process.system(cmd, ignore_status=True)
            self.user = 'testacl'
        self._set_polkit_conf()
        # Polkit rule will take about 1 second to take effect after change.
        # Restart polkit daemon will force it immediately.
        self.polkitd.restart()

    def cleanup(self):
        """
        Cleanup polkit config
        """
        try:
            if os.path.exists(self.polkit_rules_path):
                os.unlink(self.polkit_rules_path)
            if os.path.exists(self.libvirtd_backup_path):
                os.rename(self.libvirtd_backup_path, self.libvirtd_path)
            if self.user.count('EXAMPLE'):
                logging.debug("Delete the created user 'testacl'.")
                cmd = "userdel -r testacl"
                process.system(cmd, ignore_status=True)
        except Exception:
            raise PolkitConfigCleanupError("Failed to cleanup polkit config.")


class EGDConfigError(Exception):

    """
    Raise when setup local egd.pl server failed.
    """
    pass


class EGDConfig(object):

    """
    Setup egd.pl server on localhost, support startup with socket unix or tcp.
    """

    def __init__(self, params, env):
        self.params = params
        self.env = env

    def __get_tarball(self):
        tarball = "egd-0.9.tar.gz"
        tarball = self.params.get("egd_source_tarball", tarball)
        return utils_misc.get_path(data_dir.DEPS_DIR, tarball)

    def __extra_tarball(self):
        tmp_dir = data_dir.get_tmp_dir()
        tarball = self.__get_tarball()
        extra_cmd = "tar -xzvf %s -C %s" % (tarball, tmp_dir)
        process.system(extra_cmd, ignore_status=True)
        output = process.system_output("tar -tzf %s" % tarball)
        return os.path.join(tmp_dir, output.splitlines()[0])

    def startup(self, socket):
        """
        Start egd.pl server with tcp or unix socket.
        """
        if process.system("which egd.pl", ignore_status=True) != 0:
            self.install()
        prog = process.system_output("which egd.pl")
        pid = self.get_pid(socket)
        try:
            if not pid:
                cmd = "%s %s" % (prog, socket)
                p = process.SubProcess(cmd)
                p.start()
        except Exception, details:
            msg = "Unable to start egd.pl on localhost '%s'" % details
            raise EGDConfigError(msg)
        pid = self.get_pid(socket)
        logging.info("egd.pl started as pid: %s" % pid)
        return pid

    def install(self):
        """
        Install egd.pl from source code
        """
        pwd = os.getcwd()
        try:
            make_cmd = "perl Makefile.PL && make && make install"
            make_cmd = self.params.get("build_egd_cmd", make_cmd)
            src_root = self.__extra_tarball()
            process.system("cd %s && %s" % (src_root, make_cmd), shell=True)
        except Exception, details:
            raise EGDConfigError("Install egd.pl error '%s'" % details)
        finally:
            os.chdir(pwd)

    def get_pid(self, socket):
        """
        Check egd.pl start at socket on localhost.
        """
        cmd = "lsof %s" % socket
        if socket.startswith("localhost:"):
            cmd = "lsof -i '@%s'" % socket

        def system_output_wrapper():
            return process.system_output(cmd, ignore_status=True)
        output = wait.wait_for(system_output_wrapper, timeout=5)
        if not output:
            return 0
        pid = int(re.findall(r".*egd.pl\s+(\d+)\s+\w+", output, re.M)[-1])
        return pid

    def setup(self):
        backend = self.params["chardev_backend"]
        backend_type = self.params["%s_type" % backend]
        path = "path_%s" % backend_type
        port = "port_%s" % backend_type
        path, port = map(self.params.get, [path, port])
        sockets = port and ["localhost:%s" % port] or []
        if path:
            sockets.append(path)
        pids = filter(None, map(self.startup, sockets))
        self.env.data["egd_pids"] = pids

    def cleanup(self):
        try:
            for pid in self.env.data["egd_pids"]:
                logging.info("Stop egd.pl(%s)" % pid)
                utils_misc.signal_pid(pid, 15)

            def _all_killed():
                for pid in self.env.data["egd_pids"]:
                    if utils_misc.pid_is_alive(pid):
                        return False
                return True
            # wait port released by egd.pl
            wait.wait_for(_all_killed, timeout=60)
        except OSError:
            logging.warn("egd.pl is running")


class StraceQemu(object):

    """
    Attach strace to qemu VM processes, if enable_strace is 'yes'.
    It's useful to analyze qemu hang issue. But it will generate
    a large size logfile if it is enabled, so compressed original
    logfiles after strace process terminated to safe disk space.
    """

    def __init__(self, test, params, env):
        self.env = env
        self.test = test
        self.params = params
        self.process = path.find_command("strace")

    @property
    def root_dir(self):
        root_dir = os.path.join(self.test.debugdir, "strace")
        if not os.path.isdir(root_dir):
            os.makedirs(root_dir)
        return root_dir

    @property
    def log_tarball(self):
        return os.path.join(self.test.debugdir, "strace.tgz")

    def _generate_cmd(self, vm):
        """Generate strace start command line"""
        pid = vm.get_pid()
        template = ("{strace} -T -tt -e trace=all "
                    "-o {logfile} -p {pid}")
        logfile = os.path.join(self.root_dir, "%s.log" % pid)
        kwargs = {"strace": self.process,
                  "pid": pid,
                  "logfile": logfile}
        return template.format(**kwargs)

    def _start(self, cmd):
        """Start strace process in sub-process"""
        p = process.SubProcess(cmd)
        pid = p.start()
        try:
            self.env["strace_processes"] += [pid]
        except Exception:
            self.env["strace_processes"] = [pid]
        return self.env["strace_processes"]

    def _compress_log(self):
        """Compress and remove strace logfiles"""
        log_tarball = os.path.join(self.root_dir, self.log_tarball)
        archive.compress(log_tarball, self.root_dir)
        shutil.rmtree(self.root_dir)

    def start(self, vms=None):
        """Attach strace to qemu VM process"""
        if not vms:
            vms = [self.params["main_vm"]]
        for vm in self.env.get_all_vms():
            if vm.name not in vms:
                continue
            cmd = self._generate_cmd(vm)
            self._start(cmd)

    def stop(self):
        """Stop strace process and compress strace log file"""
        while self.env.get("strace_processes"):
            pid = self.env.get("strace_processes").pop()
            if process.pid_exists(pid):
                logging.info("stop strace process: %s" % pid)
                process.kill_process_tree(pid)
        self._compress_log()


def remote_session(params):
    """
    create session for remote host

    :param params: Test params dict for remote machine login details
    :return: remote session object
    """
    server_ip = params["server_ip"]
    server_user = params.get("server_user", "root")
    server_pwd = params["server_pwd"]
    return remote.wait_for_login('ssh', server_ip, '22', server_user,
                                 server_pwd, r"[\#\$]\s*$")


def switch_indep_threads_mode(state="Y", params=None):
    """
    For POWER8 compat mode guest to boot on POWER9 host, indep_threads_mode
    to be turned off as pre-requisite. This will be used in env_process
    for pre/post processing.

    :param state: 'Y' or 'N' default: 'Y'
    :param params: Test params dict for remote machine login details
    """
    indep_threads_mode = "/sys/module/kvm_hv/parameters/indep_threads_mode"
    cmd = "cat %s" % indep_threads_mode
    if params:
        server_session = remote_session(params)
        cmd_output = server_session.cmd_status_output(cmd)
        if cmd_output[0] != 0:
            server_session.close()
            raise exceptions.TestSetupFail("Unable to get indep_threads_mode:"
                                           " %s" % cmd_output[1])
        thread_mode = cmd_output[1].strip()
    else:
        try:
            thread_mode = process.system_output(cmd, shell=True)
        except process.CmdError as info:
            thread_mode = info.result.stderr.strip()
            raise exceptions.TestSetupFail("Unable to get indep_threads_mode "
                                           "for power9 compat mode enablement"
                                           ": %s" % thread_mode)
    if thread_mode != state:
        cmd = "echo %s > %s" % (state, indep_threads_mode)
        if params:
            server_user = params.get("server_user", "root")
            if (server_user.lower() != "root"):
                raise exceptions.TestSkipError("Turning indep_thread_mode %s "
                                               "requires root privileges "
                                               "(currently running "
                                               "with user %s)" % (state,
                                                                  server_user))
            cmd_output = server_session.cmd_status_output(cmd)
            server_session.close()
            if (cmd_output[0] != 0):
                raise exceptions.TestSetupFail("Unable to turn "
                                               "indep_thread_mode "
                                               "to %s: %s" % (state,
                                                              cmd_output[1]))
            else:
                logging.debug("indep_thread_mode turned %s successfully "
                              "in remote server", state)
        else:
            try:
                utils_misc.verify_running_as_root()
                process.run(cmd, verbose=True, shell=True)
                logging.debug("indep_thread_mode turned %s successfully",
                              state)
            except process.CmdError, info:
                raise exceptions.TestSetupFail("Unable to turn "
                                               "indep_thread_mode to "
                                               "%s: %s" % (state, info))


def switch_smt(state="off", params=None):
    """
    Checks whether smt is on/off, if so disables/enable it in PowerPC system
    This function is used in env_process and in libvirt.py to check
    & disable smt in Powerpc for local and remote machine respectively.

    :param state: 'off' or 'on' default: off
    :param params: Test params dict for remote machine login details
    """
    SMT_DISABLED_STRS = ["SMT is off", "Machine is not SMT capable"]
    cmd = "ppc64_cpu --smt"
    if params:
        server_session = remote_session(params)
        cmd_output = server_session.cmd_status_output(cmd)
        smt_output = cmd_output[1].strip()
        if (cmd_output[0] != 0) and (smt_output not in SMT_DISABLED_STRS):
            server_session.close()
            raise exceptions.TestSetupFail("Couldn't get SMT of server: %s"
                                           % cmd_output[1])
    else:
        try:
            smt_output = process.system_output(cmd, shell=True).strip()
        except process.CmdError as info:
            smt_output = info.result.stderr.strip()
            if smt_output not in SMT_DISABLED_STRS:
                raise exceptions.TestSetupFail("Couldn't get SMT of server: %s"
                                               % smt_output)
    smt_enabled = smt_output not in SMT_DISABLED_STRS
    if (state == "off" and smt_enabled) or (state == "on" and not smt_enabled):
        cmd = "ppc64_cpu --smt=%s" % state
        if params:
            server_user = params.get("server_user", "root")
            if (server_user.lower() != "root"):
                raise exceptions.TestSkipError("Turning SMT %s requires root "
                                               "privileges(currently running "
                                               "with user %s)" % (state,
                                                                  server_user))
            cmd_output = server_session.cmd_status_output(cmd)
            server_session.close()
            if (cmd_output[0] != 0):
                raise exceptions.TestSetupFail("Unable to turn %s SMT :%s"
                                               % (state, cmd_output[1]))
            else:
                logging.debug("SMT turned %s successfully in remote server",
                              state)
        else:
            try:
                utils_misc.verify_running_as_root()
                process.run(cmd, verbose=True, shell=True)
                logging.debug("SMT turned %s successfully", state)
            except process.CmdError, info:
                raise exceptions.TestSetupFail("Unable to turn %s SMT :%s" %
                                               (state, info))


class LibvirtdDebugLog(object):
    """
    Enable libvirtd log for testcase incase
    with the use of param "enable_libvirtd_debug_log",
    with additional params log level("libvirtd_debug_level")
    and log file path("libvirtd_debug_file") can be controlled.
    """
    def __init__(self, test, log_level=1, log_file=""):
        """
        initialize variables

        :param test: Test object
        :param log_level: debug level for libvirtd log
        :param log_file: debug file path
        """
        self.log_level = log_level
        self.log_file = log_file
        self.test = test
        self.libvirtd = utils_libvirtd.Libvirtd()
        self.libvirtd_conf = utils_config.LibvirtdConfig()
        self.backupfile = "%s.bkup.debuglog" % self.libvirtd_conf.conf_path

    def enable(self):
        """ Enable libvirtd debug log """
        if not self.log_file or not os.path.isdir(os.path.dirname(self.log_file)):
            self.log_file = utils_misc.get_path(self.test.debugdir,
                                                "libvirtd.log")
        logging.debug("libvirtd debug log stored in: %s", self.log_file)
        self.libvirtd_conf["log_level"] = self.log_level
        self.libvirtd_conf["log_outputs"] = '"%s:file:%s"' % (self.log_level,
                                                              self.log_file)
        self.libvirtd.restart()
        fd = open(self.backupfile, "w")
        fd.write(self.libvirtd_conf.backup_content)
        fd.close()

    def disable(self):
        """ Disable libvirtd debug log """
        os.rename(self.backupfile, self.libvirtd_conf.conf_path)
        self.libvirtd.restart()

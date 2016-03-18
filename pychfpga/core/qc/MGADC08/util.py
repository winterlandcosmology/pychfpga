
import sys
import os
import re
import datetime
import git  # pip install gitpython

#  ... more imports below

def add_paths(*paths):
    """ Add folders to the current search path.

    This is needed to access packages that are in folders above the one from which we run scripts.
    This function makes sure that the paths are not duplicated.
    """
    for path in paths:
        fullpath = os.path.realpath(path)
        if fullpath not in sys.path:
            sys.path.insert(1, fullpath)

add_paths('..')  # needed to find labpy
add_paths('../..')  # needed to find icecore

import labpy  # McGill's GPIB & LAN instrument library
import icecore
from icecore import NameSpace, XReport

def run_tests(config_file):
    cfg = load_config(config_file)

    test_results_folder = cfg.test_results_folder
    mgadc08_model_number = cfg.mgadc08_model_number

    # instr = open_instruments(cfg.instruments, ['dmm'])
    # instr.dmm.display('Hello', 'SCAN serial number')
    current_serial = cfg.debug.default_serial
    current_model = cfg.debug.default_model
    is_serial_scanned = False
    test_list = NameSpace(cfg.test_list)  # convert list of (key,values) into an OrderedDict


    # load test menu and convert each dict element to NameSpace to simplify code
    test_menu = [NameSpace(menu_item) for menu_item in cfg.menu]


    # Update menu with information from the test list
    for menu_item in test_menu:
        if menu_item.type == 'test':
            test = test_list[menu_item.test_tag]
            if not menu_item.get('description', None):  # Take description from the tets list if there is none
                menu_item.description = test.description
            menu_item.regex = '(%s|%s)' % (test.path, menu_item.test_tag) # have the menu recognize the test path or key as an other way to select the test

    while True:
        print
        print
        print '---------------------------------------------'
        if current_model and current_serial:
            print 'Currently testing  %s SN%s (Scanned=%s)' % (current_model, current_serial, is_serial_scanned)
        else:
            print ' !!! NO SERIAL NUMBER CURRENTLY SELECTED !!!'
        print '---------------------------------------------'
        print
        # Get a summary of all tests run so far
        test_folder = os.path.join(test_results_folder, '%s_SN%s' % (current_model, current_serial))
        summary = XReport.generate_test_summary(input_folder=test_folder, required_tests=cfg.test_list)

        # Update the menu to indicate which tests have passed.
        # Also use that information to determine the next test to run by default.
        default_choice = None
        for menu_item in test_menu:
            if menu_item.type == 'test':
                test_tag = menu_item.test_tag
                passed_list = summary.get_passed(test_list[test_tag].path)
                dependents_ok = check_test_dependencies(test_tag, test_list, summary)
                if not passed_list:
                    menu_item.status = ' ?  '
                    if not default_choice and dependents_ok:
                        default_choice = menu_item.key
                elif all(passed_list):
                    menu_item.status = 'PASS'
                else:
                    menu_item.status = 'FAIL'
                    if not default_choice and dependents_ok:
                        default_choice = menu_item.key
                menu_item.status += ', Ready' if dependents_ok and current_model and current_serial else ''
        if not default_choice or not current_model or not current_serial:
            default_choice = 'Q'

        selection = select_menu_item(test_menu, default=default_choice)
        print

        if selection.type == 'exit':
            # instr.dmm.display('Bye!', '')
            break
        elif selection.type == 'board_info':
            if selection.model not in mgadc08_model_number:
                print
                print '!!!! This is not a valid serial number for this test. Try again.'
            else:
                current_model = selection.model
                current_serial = selection.serial
                is_serial_scanned = True
                default_choice = selection.next_key
                # instr.dmm.display(current_model, current_serial)
        elif selection.type == 'push':
            commit_repo(test_results_folder)
        elif selection.type == 'summary':
            test_folders = os.path.join(test_results_folder, '%s_SN*/' % (current_model))
            f = raw_input('Enter destination filename (optional, no extension):')
            summary_filename = os.path.join(test_results_folder, f+'.csv')
            XReport.generate_combined_summary(test_folders, cfg.test_list, output_filename=summary_filename)
        elif selection.type == 'test':
            test = test_list[selection.test_tag]

            if test.require_serial_number and (not is_serial_scanned or not current_serial or not current_model):
                print
                print '!!!!!!!!!!!!!!!!!!'
                print 'Please enter or scan a serial number before beginning a test'
                continue
            # summary_data = xr.generate_summary(summary_filename, data_folder)
            nose_test_path = test.path
            test_date = datetime.datetime.now().isoformat().replace(':','_')+'_' if not cfg.debug.no_date else ''


            print
            print 'Running test %s' % nose_test_path
            print

            if test.require_serial_number:
                serial = current_serial
            else:
                serial = None
                is_serial_scanned = False

            params = NameSpace(config_file=config_file, model=current_model, serial=serial)
            r = XReport.run(nose_test_path, xparams=params)
            print
            print

            if params.serial != current_serial:
                print '**********************************************'
                print ' **** Serial number is now %s' % (params.serial)
                print '**********************************************'
                current_serial = params.serial
                is_serial_scanned = False

            test_folder = os.path.join(test_results_folder, '%s_SN%s' % (current_model, current_serial))
            test_data_folder = os.path.join(test_folder, 'data')
            test_file_name = '%s%s.pdf' %  (test_date, nose_test_path.replace(':', '.'))
            full_test_filename = os.path.join(test_data_folder, test_file_name)

            if not os.path.exists(test_folder):
                print 'Creating folder %s' % test_data_folder
                os.makedirs(test_data_folder)

            # print 'Test data will be stored in %s' % full_test_filename
            r.write(filename=full_test_filename, formats=cfg.xformat)  # Write the test reports and data
            # if r.passed:
            #     default_choice = selection.next_key
            # else:
            #     default_choice = 'Q'
            print 'Generating summary report...'
            summary_file_name = os.path.join(test_folder, '%s_SN%s_summary.pdf' %  (current_model, current_serial))
            t = XReport.generate_test_summary(input_folder=test_data_folder, required_tests=cfg.test_list, output_filename=summary_file_name, title='%s_SN%s Summary Test Report' % (current_model, current_serial))
    return locals()  # return a dict of all local variables to help interactive debugging


def check_test_dependencies(test_tag, test_list, test_summary):
    test = test_list[test_tag]
    dependencies = test.get('dependencies', []) or []   # return an empty list if the dependency is not there or is None
    if not isinstance(dependencies, list):
        dependencies = [dependencies]
    passed = True
    for dep_tag in dependencies:
        passed_list = test_summary.get_passed(test_list[dep_tag].path)
        passed = passed and bool(passed_list) and all(passed_list) and check_test_dependencies(dep_tag, test_list, test_summary)
    return passed


def select_menu_item(menu, default=''):
    """
    Prints a menu and ask the user to select an item.

    ``menu`` is a list of items. Each item is a list of [key, description, user_args], where the first element
    is the key and the second is the description.

    If ``pattern_list`` is specified, and if the used input matches the
    specified pattern, the function returns [None, None, parsed_pattern_dict],
    where parsed_pattern_dict is a dict containing the parsed pattern is
    returned instead.

    Returns the list corresponding to the selected item.
    """
    print 'Select the operation to execute.\n'

    key_width = 0
    status_width = 0
    for menu_item in menu:
        key_width = max(key_width, len(menu_item.get('key', '')))
        status_width = max(status_width, len(menu_item.get('status', '')))
    fmt = ('%%-%is  ' % status_width)  + ('%%-%is. ' % key_width) + '%s'
    for item in menu:
        if 'description' in item:
            # if 'key' in item:
                print fmt % (item.get('status',''), item.get('key',''), item.get('description',''))
            # else:
            #     print "    %s" % item['description']

    while True:
        choice = raw_input("Enter choice%s: " % (' [%s]' % default if default else '')).strip()
        pattern_match = find_menu_item(choice or default, menu)
        if pattern_match:
            return pattern_match
        print "Choice is not valid. Valid choices are %s. Please try again." % (', '.join(item['key'] for item in menu if item.get('key', None)))

def find_menu_item(text, pattern_list):
    """ Finds the menu item whose 'key' or 'regex' pattern matches the specified text.
    Returns None if no pattern matched.
    """

    def string(matches, value):
        return value

    def regex_group(matches, index):
        return matches.group(index)

    for pattern in pattern_list:
        # print pattern
        pattern = pattern.copy()
        if 'regex' in pattern:
            matches = re.match(pattern['regex'] + '$', text, re.I)
            if matches:
                args = {}
                for key, value in pattern.items():
                    if isinstance(value, str) and key not in ('key', 'regex'):  # regex has curly braces...
                        # print matches.groups(), value, key
                        # sanitized_value = value.replace('{','{{').replace('}','}}')
                        args[key] = value.format(*matches.groups())
                    else:
                        args[key] = value
                    # elif isinstance(value, int):
                    #     args[key] = group(matches, value)
                    # elif isinstance(value, dict):
                    #     method = locals()[value.pop('method')]
                    #     args[key] = method(matches=matches, **value)
                    # elif isinstance(value, list):
                    #     method = locals()[value[0]]
                    #     args[key] = method(matches, *value[1:])
                return NameSpace(args)
        if 'key' in pattern:
            if text.lower() == pattern['key'].lower():
                return NameSpace(pattern)
    return None


def load_config(filename):
        print 'Loading config file %s' % filename
        with open(filename, 'rb') as yamlfile:
            cfg = NameSpace(icecore.load_yaml(yamlfile))
        return cfg

def open_instruments(instruments, filter_list=None):
    """ Create and open objects representing instruments.

    ``instruments`` is a dict containing the list of instruments. Each entry is in the form:
        instrument_name : {labpy_object: my_labpy_object, ...}

    where labpy_object is the mandatory field that describes the name of the
    labpy class used to create the instrument. All other keywoards areguments
    are passed to that class' constructor to create the object.

    Returns a dict-like NameSpace of instrument objects with keys identical to those provided in the instrument list.
    """
    # opening communication with instruments
    if filter_list:
        instruments = NameSpace((key, instruments[key]) for key in filter_list)

    instr = NameSpace()

    for instr_name, connection_parameters in instruments.items():
        # print instr_name, connection_parameters
        labpy_object_name = connection_parameters.pop('labpy_object')
        labpy_object = getattr(labpy, labpy_object_name)
        # print labpy_object
        instr[instr_name] = labpy_object(**connection_parameters)
    return instr

def get_repo(repo_name='ch_acq'):
    ''' Just returns an instance of the specified git repository
    :return: Repo object for the specified repository (iceboard_qc or ch_acq or chFPGA, though not all implemented yet)
    '''
    import git
    if repo_name.lower() == 'chfpga':
        return git.Repo('../../../../../../chFPGA')
    elif repo_name.lower() == 'ch_acq':
        return git.Repo('../../../../../')
    elif repo_name.lower() == 'iceboard-qc':
        directory = os.path.dirname(read_config()['results_directory'].rstrip('/'))
        return git.Repo(directory)
    elif repo_name.lower() == 'hardware_tracking':
        directory = read_config()['hw_track_directory'].rstrip('/')
        return git.Repo(directory)
    else:
        raise Exception("Unknown git reporitory: " + repo_name)


def pull_all( ):
    import traceback
    from git import GitCommandError
    # For results repo
    # Check for no_commit file
    if os.path.exists(os.path.join(read_config()['results_directory'], 'no_commit')):
        print "Skipping git pull of results due to presence of 'no_commit' file in results directory."
    else:
        try:
            res_repo = get_repo('iceboard-qc')
            res_repo.git.pull('--rebase')
            print "Pulled latest version of results git repository."
        except GitCommandError:
            "Failed to pull latest version of results git repository. (trace below)"
            traceback.print_exc()
            "\nPlease run and exit the test suite again, or commit and push the changes manually."
    # For tracking repo
    # Check for no_commit file
    if os.path.exists(os.path.join(read_config()['hw_track_directory'], 'no_commit')):
        print "Skipping git commit of hardware tracking due to presence of 'no_commit' file in results directory."
    else:
        try:
            hw_repo = get_repo('hardware_tracking')
            hw_repo.git.pull('--rebase')
            print "Pulled latest version of hardware tracking git repository."
        except GitCommandError:
            "Failed to pull latest version of hardware tracking git repository. (trace below)"
            traceback.print_exc()
            "\nPlease run and exit the test suite again, or commit and push the changes manually."


def commit_repo(repo_path):
    import traceback

    repo = git.Repo(repo_path)

    try:
        if repo.is_dirty() or len(repo.untracked_files) > 0:
            to_stage = [diff.a_blob.path for diff in repo.index.diff(None)] + repo.untracked_files
            repo.index.add(to_stage)
            print "Added modified files to results git index:"
            for file in to_stage:
                print "    " + file
            repo.index.commit("Changes to QC results committed from testing script.")
            print "Changes committed. Pulling & merging latest results from server..."
            repo.git.pull('--rebase')
            print "Pulled from origin. Pushing data to server"
            repo.remotes.origin.push()
            print "New commit pushed to server!"
        else:
            print "Local Repository is clean. Nothing to commit!"
            print "Pulling latest results from server..."
            repo.git.pull('--rebase')
            print "Pulled from sever"
            # repo.remotes.origin.push()
            # print "New commit pushed to server!"

    except git.GitCommandError:
        print "Failed to add files to 'iceboard-qc' git repository. (trace below)"
        traceback.print_exc()

def commit_hw_files( ):
    import traceback
    from git import GitCommandError

    # Check for no_commit file
    if os.path.exists(os.path.join(read_config()['hw_track_directory'], 'no_commit')):
        print "Skipping git commit of results due to presence of 'no_commit' file in hardware_tracking directory."
        return None

    hw_repo = get_repo('hardware_tracking')
    try:
        if hw_repo.is_dirty() or len(hw_repo.untracked_files) > 0:
            to_stage = [diff.a_blob.path for diff in hw_repo.index.diff(None)] + hw_repo.untracked_files
            hw_repo.index.add(to_stage)
            print "Added modified files to hardware tracking git index:"
            for file in to_stage:
                print "    " + file
            hw_repo.index.commit("Update to Iceboard tracking committed from testing script.")
            print "Changes committed.\n"
            hw_repo.git.pull('--rebase')
            print "Pulled from origin..."
            hw_repo.remotes.origin.push()
            print "New commit pushed to origin!"
        else:
            "Repository is clean. Nothing to commit!"

    except GitCommandError:
        "Failed to add files to 'hardware_tracking' git repository. (trace below)"
        traceback.print_exc()
        "\nPlease run and exit the test suite again, or commit and push the changes manually."

#Common date formatting for testing functions
def date_format(date, short = False):
    '''
    formats date: converts to string and adds 0 if <10 for day, month
    (legacy function)
    '''
    #day
    day=str(date[2])
    if int(day)<10:
        day ='0' + day
    #month
    month=str(date[1])
    if int(month)<10:
        month ='0' + month
    #year
    year=str(date[0])
    #hour
    hour=str(date[3])
    if int(hour)<10:
       hour ='0' + hour
    #minutes
    minute=str(date[4])
    if int(minute)<10:
        minute ='0' + minute

    if short: # return 'dd/mm/yy'
        return day + '/' + month + '/' + year[2:4]
    else: # return 'dd/mm/yyyy, hh:mm'
        return day + '/' + month + '/' + year + ', ' + hour + ':' + minute
'''
Collection of functions used by many of the scripts for variety of things,
but that don't fit into any of the other modules.
'''

import os

def read_config():
    '''
    Simply reads the 'config.yaml' file, or 'config_example.yaml' if there is no user configure one.
    If the example is read, the 'default_config' key is set to True.
    :return: Dictionary of config parameters. An extra key 'default_config' is True if no 'config.yaml' file was found,
     indicating that 'config_example.yaml' was used instead.
    '''
    import yaml
    import os

    if os.path.isfile('config.yaml'):
        config = yaml.load(open('config.yaml'))
        config['default_config'] = False
    else:
        config = yaml.load(open('config_example.yaml'))
        config['default_config'] = True
    return config

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


def commit_results( ):
    import traceback
    from git import GitCommandError

    # Check for no_commit file
    if os.path.exists(os.path.join(read_config()['results_directory'], 'no_commit')):
        print "Skipping git commit of results due to presence of 'no_commit' file in results directory."
        return None

    res_repo = get_repo('iceboard-qc')
    try:
        if res_repo.is_dirty() or len(res_repo.untracked_files) > 0:
            to_stage = [diff.a_blob.path for diff in res_repo.index.diff(None)] + res_repo.untracked_files
            res_repo.index.add(to_stage)
            print "Added modified files to results git index:"
            for file in to_stage:
                print "    " + file
            res_repo.index.commit("Changes to QC results committed from testing script.")
            print "Changes committed.\n"
            res_repo.git.pull('--rebase')
            print "Pulled from origin..."
            res_repo.remotes.origin.push()
            print "New commit pushed to origin!"
        else:
            "Repository is clean. Nothing to commit!"

    except GitCommandError:
        "Failed to add files to 'iceboard-qc' git repository. (trace below)"
        traceback.print_exc()
        "\nPlease run and exit the test suite again, or commit and push the changes manually."

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
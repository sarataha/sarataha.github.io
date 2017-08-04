from fabric.api import *
from fabric.colors import red, green
from time import sleep
from pprint import pprint
from fabric.contrib.files import exists


def local_git():
    is_done = True

    try:
        with lcd('/home/sara/Desktop/personal_website/'):
            r = local('git status', capture=False)
            # Make the branch is not Upto date before doing further operations

            if 'Your branch is up-to-date with' not in r.stdout:
                local('git add .', capture=True)
                local('git commit -m "automatic commit by fabric"')
                local('git push origin master')
    except Exception as e:
        print(str(e))
        is_done = False
    finally:
        return is_done


def remote_git():
    is_done = True
    try:
        with settings(host_string=host.rstrip('\n').strip(), warn_only=True):
            with cd('public_html/code'):
                run('git stash')
                run('git pull origin master')
    except Exception as e:
        print(str(e))
        is_done = True
    finally:
        return is_done


def deploy():
    result = local_git()
    if result:
        print('Time to deploy')

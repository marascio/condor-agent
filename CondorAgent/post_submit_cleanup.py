#!/usr/bin/env python
# encoding: utf-8

###### COPYRIGHT NOTICE ########################################################
#
# Copyright (C) 2007-2013, Cycle Computing, LLC.
# 
# Licensed under the Apache License, Version 2.0 (the "License"); you
# may not use this file except in compliance with the License.  You may
# obtain a copy of the License at
# 
#   http://www.apache.org/licenses/LICENSE-2.0.txt
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
################################################################################

################################################################################
# USAGE
################################################################################



################################################################################
# IMPORTS
################################################################################
import os
import re
import shutil
import pickle
import glob
import util
import logging
import threading
import time

################################################################################
# GLOBALS
################################################################################
__doc__ = """post_submit_cleanup.py

A threaded cleanup implementation. This module is launched as a separate
thread when the CondorAgent is started if, and only if, the agent is set
to do local Condor submissions.

It handles cleaning up the cruft from local Condor runs so that old output
data doesn't accumulate on disk. It runs once an hour.
"""


################################################################################
# CLASSES
################################################################################
class LocalSubmitCleaner(threading.Thread):
    
    def __init__(self, dryrun=False, sleeptime=600):
        self._dryrun = dryrun
        self._sleeptime = sleeptime
        self._stopevent = threading.Event()
        threading.Thread.__init__(self, name="LocalSubmitCleaner")
        self.daemon = True
        self.setDaemon(True)
    
    def run(self):
        '''Runs the thread in a loop, looking for clusters than can have their
        submit directories wiped from disk because they are no longer in the
        queue. Sleeps after each pass on the submit directory list.'''
        
        logging.info('[cleaner] Local submission cleanup thread starting up')
        
        # TODO Should we warn the user if they're running as root? That's dangerous.
        # TODO Should we context switch to CONDOR_IDS automatically if we're root?
        
        while not self._stopevent.isSet():
            # Case #8236: Sleep when the cleanup thread starts. Gives the schedds a
            # chance to start up and avoids a race condition that can delete the directories
            # of jobs that may actually still be in the queued.
            logging.info('[cleaner] Sleeping for %d seconds' % self._sleeptime)
            self._stopevent.wait(self._sleeptime)            
            submitDir = util.getCondorConfigVal('CONDOR_AGENT_SUBMIT_DIR', default='""').replace('"', '')
            if submitDir == '':
                logging.error('[cleaner] Could not find a CONDOR_AGENT_SUBMIT_DIR setting for this host -- no cleanup performed')
            else:
                logging.info('[cleaner] Scanning submit directory \'%s\' for *.cluster files...' % submitDir)
                for c in self._locate(pattern='*.cluster', root=submitDir):
                    # I never want this thread to exit because of an exception so we'll blanket trap
                    # everything at this level and just report it back as an error.
                    try:
                        self._safeRemoveClusterFiles(c)
                    except Exception, e:
                        logging.error('[cleaner] Caught unhandled exception: %s' % (str(e)))
    
    def _locate(self, pattern, root=os.curdir):
        '''Locate all files matching supplied filename pattern in the
        supplied directory.'''
        for matches in glob.glob(os.path.join(root, pattern)):
            yield os.path.join(root, matches)
    
    def _safeRemoveClusterFiles(self, cfile):
        '''Check to see if a cluster is still running by loading the
        data for the cluster in cfile. If it is: return False, if it
        is not running delete the cluster data files and return True.'''
        
        # Don't even think of removing this cluster if the cfile isn't
        # older than 5 minutes. This helps eliminate a race condition
        # where jobs are submitted, the pickeled data is dumped to disk,
        # but Condor hasn't completely processed the jobs and they aren't
        # showing up in the queue output from condor_q yet.
        (mode, ino, dev, nlink, uid, gid, size, atime, mtime, ctime) = os.stat(cfile)
        if time.time() - mtime < 300:
            logging.info('[cleaner] ...submission file %s is not old enough to be considered (mtime = %s)' % (os.path.split(cfile)[1], time.ctime(mtime)))
            return
        
        
        # Load the tuple that represents this cluster from cfile..
        pkl_file = open(cfile, 'rb')
        cdata = pickle.load(pkl_file)
        pkl_file.close()
        # Data dictionary contains the following keys
        # clusterid
        # queue
        # tmpdir
        logging.info('[cleaner] Checking cluster %s for jobs in queue %s...' % (cdata.get('clusterid', 'Unknown'), cdata.get('queue', 'localhost')))
        jobsInQueue = self._condorJobsInQueue(cdata)
        if jobsInQueue == 0:
            logging.info('[cleaner] ...found %d jobs in the queue, performing cleanup of directory "%s"' % (jobsInQueue, cdata.get('tmpdir')))
            # Remove the path...
            if not self._dryrun:
                try:
                    shutil.rmtree(cdata.get('tmpdir'), False)
                except Exception, e:
                    logging.error('[cleaner] Unable to remove path "%s": %s' % (cdata.get('tmpdir'), str(e)))
                else:
                    logging.info('[cleaner] ...removed path "%s"' % cdata.get('tmpdir'))
                    try:
                        os.remove(cfile)
                    except Exception, e:
                        logging.error('[cleaner] Unable to remove file "%s": %s' % (cfile, str(e)))
                    else:
                        logging.info('[cleaner] ...removed file "%s"' % cfile)
            else:
                logging.debug('[cleaner] ...DRY RUN would have removed path "%s"' % cdata.get('tmpdir'))
                logging.debug('[cleaner] ...DRY RUN would have removed file "%s"' % cfile)
        elif jobsInQueue > 0:
            logging.info('[cleaner] ...found %d jobs in the queue still, no cleanup done' % jobsInQueue)
        else:
            # We got a value of None instead of an int in [0,inf) range. That's bad.
            logging.error('[cleaner] ...unable to run condor_q to count jobs in queue, no clean up done')
    
    def _condorJobsInQueue(self, cdata):
        '''Returns the number of jobs still in the queue for a cluster.'''
        jobCount = None
        # condor_q -name q1@`hostname` -f "%d\n" ClusterID 11292
        # We don't care if jobs are in the queue in the C or X state. So filter
        # those out with a constraint.
        if cdata.get('queue'):
            cmd = ['condor_q', '-name', cdata.get('queue'), '-f', '"%d\\n"', 'ClusterID', '-c', '"JobStatus != 3 && JobStatus != 4"', cdata.get('clusterid')]
        else:
            cmd = ['condor_q', '-f', '"%d\\n"', 'ClusterID', '-c', 'JobStatus != 3 && JobStatus != 4', cdata.get('clusterid')]
        logging.info('[cleaner] ...running: %s' % ' '.join(cmd))
        (return_code, stdout_value, stderr_value) = util.runCommand2(' '.join(cmd))
        # Case #8380: Job directories are being deleted when jobs remain in the queue
        # Pre Condor 7.2.2 it's not enough to just check the return code. Condor < 7.2.2 would often set
        # the return code to 0 and write error notes to stderr. So we have to check that stderr is
        # empty as well.
        if len(stderr_value) > 0:
            logging.error('[cleaner] ...got error running command: %s' % stderr_value)
        else:
            if return_code == 0:
                # Count the lines in the output that have the cluster ID in them
                # That's the number of jobs in the queue still.
                repat = re.compile(r"^\s*%s\s*" % cdata.get('clusterid'), re.M)
                matches = re.findall(repat, stdout_value)
                jobCount = len(matches)
        return jobCount
    


################################################################################
# METHODS
################################################################################
def main():
    # TODO Fix this. It would be helpful to test if we could do this.
    print 'Error: Module cannot be called as a script at this point'
    pass

if __name__ == '__main__':
    main()


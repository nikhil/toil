#!/usr/bin/env python

"""The master component (of a master slave pattern) for a job manager used by sontrace programs (cactus etc) 
for running hierarchical trees of jobs on the cluster.

Takes a crash-only philosophy so that any part of the process can be failed and then restarted at will (see the accompanying tests).
"""

import os
import sys
import os.path 
import xml.etree.ElementTree as ET
import time

from workflow.jobTree.lib.bioio import logger, getTotalCpuTime
from workflow.jobTree.lib.bioio import getLogLevelString
from workflow.jobTree.lib.bioio import logFile
from workflow.jobTree.lib.bioio import system
from workflow.jobTree.lib.bioio import workflowRootPath

def createJob(attrib, parent, config):
    """Creates an XML record for the job in a file within the hierarchy of jobs.
    """
    job = ET.Element("job")
    job.attrib["file"] = config.attrib["job_file_dir"].getTempFile(".xml")
    job.attrib["remaining_retry_count"] = config.attrib["retry_count"]
    job.attrib["colour"] = "white"
    followOns = ET.SubElement(job, "followOns")
    ET.SubElement(followOns, "followOn", attrib.copy())
    if parent != None:
        job.attrib["parent"] = parent
    job.attrib["child_count"] = str(0)
    job.attrib["black_child_count"] = str(0)
    job.attrib["log_level"] = getLogLevelString()
    job.attrib["log_file"] = config.attrib["log_file_dir"].getTempFile(".log") #The log file for the actual command
    job.attrib["slave_log_file"] = config.attrib["slave_log_file_dir"].getTempFile(".log") #The log file for the slave
    job.attrib["global_temp_dir"] = config.attrib["temp_dir_dir"].getTempDirectory()
    job.attrib["job_creation_time"] = str(time.time())
    job.attrib["environment_file"] = config.attrib["environment_file"]
    job.attrib["job_time"] = config.attrib["job_time"]
    job.attrib["max_log_file_size"] = config.attrib["max_log_file_size"]
    job.attrib["default_memory"] = config.attrib["default_memory"]
    job.attrib["default_cpu"] = config.attrib["default_cpu"]
    job.attrib["total_time"] = attrib["time"]
    if config.attrib.has_key("stats"):
        job.attrib["stats"] = config.attrib["log_file_dir"].getTempFile(".xml") #The file to store stats in..
    ET.SubElement(job, "children") 
    return job

def deleteJob(job, config):
    """Removes an old job, including any log files.
    """
    config.attrib["log_file_dir"].destroyTempFile(job.attrib["log_file"])
    config.attrib["slave_log_file_dir"].destroyTempFile(job.attrib["slave_log_file"])
    config.attrib["temp_dir_dir"].destroyTempDir(job.attrib["global_temp_dir"])
    config.attrib["job_file_dir"].destroyTempFile(job.attrib["file"])
    if config.attrib.has_key("stats"):
        config.attrib["log_file_dir"].destroyTempFile(job.attrib["stats"])

def writeJobs(jobs):
    """Writes a list of jobs to file, ensuring that the previous
    state is maintained until after the write is complete
    """
    #The lock file name, by a round about mechanism
    
    #Create a unique updating name using the first file in the list
    fileName = jobs[0].attrib["file"]
    updatingFile = fileName + ".updating"
    
    #The existence of the the updating file signals we are in the process of creating an update to the state of the files
    assert not os.path.isfile(updatingFile)
    fileHandle = open(updatingFile, 'w')
    fileHandle.write(" ".join([ job.attrib["file"] + ".new" for job in jobs ]))
    fileHandle.close()
    
    #Update the current files.
    for job in jobs:
        newFileName = job.attrib["file"] + ".new"
        assert not os.path.isfile(newFileName)
        fileHandle = open(newFileName, 'w')
        tree = ET.ElementTree(job)
        tree.write(fileHandle)
        fileHandle.close()
       
    os.remove(updatingFile) #Remove the updating file, now the new files represent the valid state
    
    for job in jobs:
        if os.path.isfile(job.attrib["file"]):
            os.remove(job.attrib["file"])
        os.rename(job.attrib["file"] + ".new", job.attrib["file"])

def issueJobs(jobs, jobIDsToJobsHash, batchSystem):
    """Issues jobs to the batch system.
    """
    jobCommands = {}
    for job in jobs:
        jobCommand = os.path.join(workflowRootPath(), "bin", "jobTreeSlave")
        followOnJob = job.find("followOns").findall("followOn")[-1]
        jobCommands["%s -E %s %s --job %s" % (sys.executable, jobCommand, os.path.split(workflowRootPath())[0], job.attrib["file"])] = (job.attrib["file"], int(followOnJob.attrib["memory"]), int(followOnJob.attrib["cpu"]), job.attrib["slave_log_file"])
    issuedJobs = batchSystem.issueJobs([ (key, jobCommands[key][1], jobCommands[key][2], jobCommands[key][3]) for key in jobCommands.keys() ])
    assert len(issuedJobs.keys()) == len(jobCommands.keys())
    for jobID in issuedJobs.keys():
        command = issuedJobs[jobID]
        jobFile = jobCommands[command][0]
        assert jobID not in jobIDsToJobsHash
        jobIDsToJobsHash[jobID] = jobFile
        logger.debug("Issued the job: %s with job id: %i " % (jobFile, jobID))

def fixJobsList(config, jobFiles):
    """Traverses through and finds any .old files, using there saved state to recover a 
    valid state of the job tree.
    """
    updatingFiles = []
    for fileName in jobFiles[:]:
        if "updating" in fileName:
            updatingFiles.append(fileName)
    
    for updatingFileName in updatingFiles: #Things crashed while the state was updating, so we should remove the 'updating' and '.new' files
        fileHandle = open(updatingFileName, 'r')
        for fileName in fileHandle.readline().split():
            if os.path.isfile(fileName):
                config.attrib["job_file_dir"].destroyTempFile(fileName)
                jobFiles.remove(fileName)
        fileHandle.close()
        config.attrib["job_file_dir"].destroyTempFile(updatingFileName)
        jobFiles.remove(updatingFileName)
    
    for fileName in jobFiles[:]: #Else any '.new' files will be switched in place of the original file. 
        if fileName[-3:] == 'new':
            originalFileName = fileName[:-4]
            os.rename(fileName, originalFileName)
            jobFiles.remove(fileName)
            if originalFileName not in jobFiles:
                jobFiles.append(originalFileName)
            logger.critical("Fixing the file: %s from %s" % (originalFileName, fileName))

def restartFailedJobs(config, jobFiles):
    """Traverses through the file tree and resets the restart count of all jobs.
    """
    for absFileName in jobFiles:
        if os.path.isfile(absFileName):
            job = ET.parse(absFileName).getroot()
            logger.info("Restarting job: %s" % job.attrib["file"])
            job.attrib["remaining_retry_count"] = config.attrib["retry_count"]
            if job.attrib["colour"] == "red":
                job.attrib["colour"] = "white"
            #Is leaf and job failed when the system went downbut the status did not get updated.
            if job.attrib["colour"] == "grey": 
                job.attrib["colour"] = "white"
            writeJobs([ job ])

def processFinishedJob(jobID, resultStatus, updatedJobFiles, jobIDsToJobsHash):
    """Function reads a processed job file and updates it state.
    """
    assert jobID in jobIDsToJobsHash
    jobFile = jobIDsToJobsHash.pop(jobID)
    
    if resultStatus != 0: #Job not successful
        if os.path.isfile(jobFile + ".updating"): #The job failed while attempting to write the job file.
            logger.critical("There was an .updating file for the crashed job: %s" % jobFile)
            if os.path.isfile(jobFile + ".new"): #The job failed while writing the updated job file.
                logger.critical("There was an .new file for the crashed job: %s" % jobFile)
                os.remove(jobFile + ".new") #The existance of the .updating file means it wasn't complete
            os.remove(jobFile + ".updating") #Delete second the updating file second to preserve a correct state
            assert os.path.isfile(jobFile)
            job = ET.parse(jobFile).getroot() #The original must still be there.
            assert job.find("children").find("child") == None #The original can not reflect the end state of the job.
            assert int(job.attrib["black_child_count"]) == int(job.attrib["child_count"])
            job.attrib["colour"] = "red" #It failed, so we mark it so and continue.
            writeJobs([ job ])
            logger.critical("We've reverted to the original job file and marked it as failed: %s" % jobFile)
        else:
            if os.path.isfile(jobFile + ".new"): #The job was not properly updated before crashing
                logger.critical("There is a valid .new file %s" % jobFile)
                if os.path.isfile(jobFile):
                    os.remove(jobFile)
                os.rename(jobFile + ".new", jobFile)
                job = ET.parse(jobFile).getroot()
                assert job.attrib["colour"] in ("black", "red")
            else:
                logger.critical("There was no valid .new file %s" % jobFile)
                assert os.path.isfile(jobFile)
                job = ET.parse(jobFile).getroot() #The job may have failed before or after creating this file, we check the state.
                if job.attrib["colour"] == "black": #The job completed okay, so we'll keep it
                    logger.critical("Despite the batch system job failing, the job appears to have completed okay")
                else:
                    assert job.attrib["colour"] in ("grey", "red")
                    assert job.find("children").find("child") == None #File 
                    assert int(job.attrib["black_child_count"]) == int(job.attrib["child_count"])
                    if job.attrib["colour"] == "grey":
                        job.attrib["colour"] = "red"
                        writeJobs([ job ])
                    logger.critical("We've reverted to the original job file and marked it as failed: %s" % jobFile)
    else:
        job = ET.parse(jobFile).getroot()
        
    ##Now check the log files exist, because they must ultimately be cleaned up by their respective file trees.
    if not os.path.isfile(job.attrib["log_file"]): #We need to keep these files in existence.
        open(job.attrib["log_file"], 'w').close()
        logger.critical("The log file %s for job %s had disappeared" % (job.attrib["log_file"], jobFile))
    if not os.path.isfile(job.attrib["slave_log_file"]):
        open(job.attrib["slave_log_file"], 'w').close()
        logger.critical("The slave log file %s for job %s had disappeared" % (job.attrib["slave_log_file"], jobFile))
    
    assert jobFile not in updatedJobFiles
    updatedJobFiles.add(jobFile) #Now we know the job is done we can add it to the list of updated job files
    logger.debug("Added job: %s to active jobs" % jobFile)

def reissueOverLongJobs(updatedJobFiles, jobIDsToJobsHash, config, batchSystem):
    """Check each issued job - if it is running for longer than desirable.. issue a kill instruction.
    Wait for the job to die then we pass the job to processFinishedJob.
    """
    maxJobDuration = float(config.attrib["max_job_duration"])
    if maxJobDuration < 10000000: #We won't both doing anything is the rescue time is more than 10 weeks.
        runningJobs = batchSystem.getRunningJobIDs()
        for jobID in runningJobs.keys():
            if runningJobs[jobID] > maxJobDuration:
                logger.critical("The job: %s has been running for: %s seconds, more than the max job duration: %s, we'll kill it" % \
                            (jobIDsToJobsHash[jobID], str(runningJobs[jobID]), str(maxJobDuration)))
                batchSystem.killJobs([ jobID ])
                processFinishedJob(jobID, 1, updatedJobFiles, jobIDsToJobsHash)

reissueMissingJobs_missingHash = {} #Hash to store number of observed misses
def reissueMissingJobs(updatedJobFiles, jobIDsToJobsHash, batchSystem, killAfterNTimesMissing=3):
    """Check all the current job ids are in the list of currently running batch system jobs. 
    If a job is missing, we mark it as so, if it is missing for a number of runs of 
    this function (say 10).. then we try deleting the job (though its probably lost), we wait
    then we pass the job to processFinishedJob.
    """
    runningJobs = set(batchSystem.getIssuedJobIDs())
    jobIDsSet = set(jobIDsToJobsHash.keys())
    assert runningJobs.issubset(jobIDsSet)
    for jobID in set(jobIDsSet.difference(runningJobs)):
        jobFile = jobIDsToJobsHash[jobID]
        if reissueMissingJobs_missingHash.has_key(jobID):
            reissueMissingJobs_missingHash[jobID] = reissueMissingJobs_missingHash[jobID]+1
        else:
            reissueMissingJobs_missingHash[jobID] = 1
        timesMissing = reissueMissingJobs_missingHash[jobID]
        logger.critical("Job %s with id %i is missing for the %i time" % (jobFile, jobID, timesMissing))
        if timesMissing == killAfterNTimesMissing:
            reissueMissingJobs_missingHash.pop(jobID)
            batchSystem.killJobs([ jobID ])
            processFinishedJob(jobID, 1, updatedJobFiles, jobIDsToJobsHash)
    if len(reissueMissingJobs_missingHash) > 0:
        logger.critical("Going to sleep before trying again before trying to find missing jobs")
        time.sleep(60)
        reissueMissingJobs(updatedJobFiles, jobIDsToJobsHash, batchSystem, killAfterNTimesMissing)
            
def pauseForUpdatedJobs(updatedJobsFn, sleepFor=0.1, sleepNumber=100):
    """Waits sleepFor seconds while there are no updated jobs, repeating this 
    cycle sleepNumber times.
    """
    i = 0
    while i < sleepNumber:
        updatedJobs = updatedJobsFn()
        if len(updatedJobs) != 0:
            return updatedJobs
        time.sleep(sleepFor)
        i += 1
    return updatedJobsFn()

def mainLoop(config, batchSystem):
    """This is the main loop from which jobs are issued and processed.
    """    
    waitDuration = float(config.attrib["wait_duration"])
    assert waitDuration >= 0
    rescueJobsFrequency = float(config.attrib["rescue_jobs_frequency"])
    maxJobDuration = float(config.attrib["max_job_duration"])
    assert maxJobDuration >= 0
    logger.info("Got parameters, wait duration %s, rescue jobs frequency: %s max job duration: %s" % \
                (waitDuration, rescueJobsFrequency, maxJobDuration))
    
    #Kill any jobs on the batch system queue from the last time.
    assert len(batchSystem.getIssuedJobIDs()) == 0 #Batch system must start with no active jobs!
    logger.info("Checked batch system has no running jobs and no updated jobs")
    
    jobFiles = config.attrib["job_file_dir"].listFiles()
    logger.info("Got a list of job files")
    
    #Repair the job tree using any .old files
    fixJobsList(config, jobFiles)
    logger.info("Fixed the job files using any .old files")
    
    #Get jobs that were running, or that had failed reset to 'white' status
    restartFailedJobs(config, jobFiles)
    logger.info("Reworked failed jobs")
    
    updatedJobFiles = set() #Jobs whose status needs updating, either because they have finished, or because they need to be started.
    for jobFile in jobFiles:
        job = ET.parse(jobFile).getroot()
        if job.attrib["colour"] not in ("grey", "blue"):
            updatedJobFiles.add(jobFile)
    logger.info("Got the active (non grey/blue) job files")
    
    totalJobFiles = len(jobFiles) #Total number of job files we have.
    jobIDsToJobsHash = {} #A hash of the currently running jobs ids, made by the batch system.
    
    idealJobTime = float(config.attrib["job_time"]) 
    assert idealJobTime > 0.0
    
    maxIssuedJobs = int(config.attrib["max_jobs"]) #The maximum number of jobs to issue to the batch system
    assert maxIssuedJobs >= 1
    
    stats = config.attrib.has_key("stats")
    if stats:
        startTime = time.time()
        startClock = getTotalCpuTime()
    
    logger.info("Starting the main loop")
    timeSinceJobsLastRescued = time.time() + rescueJobsFrequency - 100 #We hack it so that we rescue jobs after the first 100 seconds to get around an apparent parasol bug
    while True:
        if len(updatedJobFiles) > 0:
            logger.debug("Built the jobs list, currently have %i job files, %i jobs to update and %i jobs currently issued" % (totalJobFiles, len(updatedJobFiles), len(jobIDsToJobsHash)))
        
        for jobFile in list(updatedJobFiles):
            job = ET.parse(jobFile).getroot()
            assert job.attrib["colour"] not in ("grey", "blue")
            
            if job.attrib["colour"] == "white": #Get ready to start the job
                if len(jobIDsToJobsHash) < maxIssuedJobs:
                    logger.debug("Job: %s is being started" % job.attrib["file"])
                    updatedJobFiles.remove(job.attrib["file"])
                    
                    #Reset the log files for the job.
                    open(job.attrib["slave_log_file"], 'w').close()
                    open(job.attrib["log_file"], 'w').close()
                    
                    job.attrib["colour"] = "grey"
                    writeJobs([ job ]) #Check point, do this before issuing job, so state is not read until issued
                    
                    issueJobs([ job ], jobIDsToJobsHash, batchSystem)
                else:
                    logger.debug("Job: %s is not being issued yet because we have %i jobs issued" % (job.attrib["file"], len(jobIDsToJobsHash)))
            elif job.attrib["colour"] == "black": #Job has finished okay
                logger.debug("Job: %s has finished okay" % job.attrib["file"])
                #Deal with stats
                if stats:
                    system("cat %s >> %s" % (job.attrib["stats"], config.attrib["stats"]))
                    open(job.attrib["stats"], 'w').close() #Reset the stats file
                childCount = int(job.attrib["child_count"])
                blackChildCount = int(job.attrib["black_child_count"])
                assert childCount == blackChildCount #Has no currently running child jobs
                #Launch any unborn children
                unbornChildren = job.find("children")
                unbornChild = unbornChildren.find("child")
                if unbornChild != None: #We must give birth to the unborn children
                    logger.debug("Job: %s has children to schedule" % job.attrib["file"])
                    newChildren = []
                    while unbornChild != None:
                        cummulativeChildTime = float(unbornChild.attrib["time"])
                        
                        newJob = createJob(unbornChild.attrib.copy(), job.attrib["file"], config)
                        
                        totalJobFiles += 1
                        updatedJobFiles.add(newJob.attrib["file"])
                        
                        newChildren.append(newJob)
                        unbornChildren.remove(unbornChild)
                        unbornChild = unbornChildren.find("child")
                        
                        #This was code to aggregate groups of children into one job, but we don't do this now
                        #while cummulativeChildTime < idealJobTime and unbornChild != None: #Cummulate a series of children into each job as a stack of jobs (to balance cost of parellelism with cost of running stuff in serially).
                        #    cummulativeChildTime += float(unbornChild.attrib["time"])
                        #    ET.SubElement(newJob.find("followOns"), "followOn", unbornChild.attrib.copy())    
                        #    unbornChildren.remove(unbornChild)
                        #    unbornChild = unbornChildren.find("child")
                        newJob.attrib["total_time"] = str(cummulativeChildTime)
                    
                    updatedJobFiles.remove(job.attrib["file"])
                    job.attrib["child_count"] = str(childCount + len(newChildren))
                    job.attrib["colour"] = "blue" #Blue - has children running.
                    writeJobs([ job ] + newChildren ) #Check point
                    
                elif len(job.find("followOns").findall("followOn")) != 0: #Has another job
                    logger.debug("Job: %s has a new command that we can now issue" % job.attrib["file"])
                    ##Reset the job run info
                    job.attrib["remaining_retry_count"] = config.attrib["retry_count"]
                    job.attrib["colour"] = "white"
                    ##End resetting the job
                    writeJobs([ job ])
                    
                else: #Job has finished, so we can defer to any parent
                    logger.debug("Job: %s is now dead" % job.attrib["file"])
                    job.attrib["colour"] = "dead"
                    if job.attrib.has_key("parent"):
                        parent = ET.parse(job.attrib["parent"]).getroot()
                        assert parent.attrib["colour"] == "blue"
                        assert int(parent.attrib["black_child_count"]) < int(parent.attrib["child_count"])
                        parent.attrib["black_child_count"] = str(int(parent.attrib["black_child_count"]) + 1)
                        if int(parent.attrib["child_count"]) == int(parent.attrib["black_child_count"]):
                            parent.attrib["colour"] = "black"
                            assert parent.attrib["file"] not in updatedJobFiles
                            updatedJobFiles.add(parent.attrib["file"])
                        writeJobs([ job, parent ]) #Check point
                    else:
                        writeJobs([ job ])
                         
            elif job.attrib["colour"] == "red": #Job failed
                logger.critical("Job: %s failed" % job.attrib["file"])
                logger.critical("The log file of the failed job")
                logFile(job.attrib["log_file"], logger.critical)
                logger.critical("The log file of the slave for the failed job")
                logFile(job.attrib["slave_log_file"], logger.critical) #We log the job log file in the main loop
                #Checks
                assert len(job.find("children").findall("child")) == 0
                assert int(job.attrib["child_count"]) == int(job.attrib["black_child_count"])
                
                remainingRetyCount = int(job.attrib["remaining_retry_count"])
                if remainingRetyCount > 0: #Give it another try, maybe there is a bad node somewhere
                    job.attrib["remaining_retry_count"] = str(remainingRetyCount-1)
                    job.attrib["colour"] = "white"
                    logger.critical("Job: %s will be restarted, it has %s goes left" % (job.attrib["file"], job.attrib["remaining_retry_count"]))
                    writeJobs([ job ]) #Check point
                else:
                    assert remainingRetyCount == 0
                    updatedJobFiles.remove(job.attrib["file"])
                    logger.critical("Job: %s is completely failed" % job.attrib["file"])
                    
            else:
                logger.debug("Job: %s is already dead, we'll get rid of it" % job.attrib["file"])
                assert job.attrib["colour"] == "dead"
                updatedJobFiles.remove(job.attrib["file"])
                totalJobFiles -= 1
                deleteJob(job, config) #This could be done earlier, but I like it this way.
                
        if len(jobIDsToJobsHash) == 0 and len(updatedJobFiles) == 0:
            logger.info("Only failed jobs and their dependents (%i total) are remaining, so exiting." % totalJobFiles)
            break
        
        if len(updatedJobFiles) > 0:
            updatedJobs = batchSystem.getUpdatedJobs() #Asks the batch system what jobs have been completed.
        else:
            updatedJobs = pauseForUpdatedJobs(batchSystem.getUpdatedJobs) #Asks the batch system what jobs have been completed.
        for jobID in updatedJobs.keys(): #Runs through a map of updated jobs and there status, 
            result = updatedJobs[jobID]
            if jobIDsToJobsHash.has_key(jobID): 
                if result == 0:
                    logger.debug("Batch system is reporting that the job %s ended sucessfully" % jobIDsToJobsHash[jobID])   
                else:
                    logger.critical("Batch system is reporting that the job %s failed with exit value %i" % (jobIDsToJobsHash[jobID], result))  
                processFinishedJob(jobID, result, updatedJobFiles, jobIDsToJobsHash)
            else:
                logger.info("A result seems to already have been processed: %i" % jobID) #T
        
        if time.time() - timeSinceJobsLastRescued >= rescueJobsFrequency: #We only rescue jobs every N seconds
            reissueOverLongJobs(updatedJobFiles, jobIDsToJobsHash, config, batchSystem)
            logger.info("Reissued any over long jobs")
            
            reissueMissingJobs(updatedJobFiles, jobIDsToJobsHash, batchSystem)
            logger.info("Rescued any (long) missing jobs")
            timeSinceJobsLastRescued = time.time()
        #Going to sleep to let the job system catch up.
        time.sleep(waitDuration)
    
    if stats:
        fileHandle = open(config.attrib["stats"], 'a')
        fileHandle.write("<total_time time='%s' clock='%s'/></stats>" % (str(time.time() - startTime), str(getTotalCpuTime() - startClock)))
        fileHandle.close()
    
    logger.info("Finished the main loop")     
    
    return totalJobFiles #Returns number of failed jobs

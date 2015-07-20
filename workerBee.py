#!/usr/bin/python

import ConfigParser
import subprocess
import sys
import time
import os
import logging
from logging.handlers import RotatingFileHandler
import requests, json
import boto
from boto.s3.key import Key
from boto.s3.connection import S3Connection
import logging
from datetime import datetime
import urllib
import socket
from twisted.internet.protocol import Protocol, ReconnectingClientFactory
from twisted.internet import pollreactor
pollreactor.install()
from twisted.internet import reactor
from twisted.internet.task import LoopingCall
import math
import urllib
from poster.encode import multipart_encode
from poster.streaminghttp import register_openers
import urllib2
from PIL import Image
from PIL import ImageFile
import os.path
import inspect, shutil

ImageFile.LOAD_TRUNCATED_IMAGES = True

#Config File Preparation
Config = ConfigParser.ConfigParser()
if(os.path.isfile('config.ini')):
	Config.read("config.ini")
else:
	Config.read("config-sample.ini")

def ConfigSectionMap(section):
    dict1 = {}
    options = Config.options(section)
    for option in options:
		try:
		    dict1[option] = Config.get(section, option)
		    if dict1[option] == -1:
		        DebugPrint("skip: %s" % option)
		except:
		    print("exception on %s!" % option)
		    dict1[option] = None
    return dict1

# Logging Setup
FORMAT = '%(asctime)-15s %(message)s'
logFile=ConfigSectionMap("WorkerBee")['logfile']
log_formatter = logging.Formatter('%(asctime)s %(levelname)s %(funcName)s(%(lineno)d) %(message)s')

my_handler = RotatingFileHandler(logFile, mode='a', maxBytes=5*1024*1024,
                                 backupCount=2, encoding=None, delay=0)
my_handler.setFormatter(log_formatter)
my_handler.setLevel(logging.DEBUG)

app_log = logging.getLogger('root')
app_log.setLevel(logging.DEBUG)
app_log.addHandler(my_handler)

##Settings from config file
hasLCD=Config.getboolean("Hardware","lcd")
queue_id=ConfigSectionMap("FabHive")['queue']
workerBeeId=ConfigSectionMap("FabHive")['workerbee']
shouldFlipCamera=Config.getboolean('Hardware','flipcamera')
katana_url=ConfigSectionMap("FabHive")['fabhiveurl']
api_key=ConfigSectionMap("FabHive")['apikey']
octoprint_api_key=ConfigSectionMap("OctoPrint")['apikey']

##Other startup settings
currentJobId = 0
printingStatus={}
isPrinting=False
octoprintAPIVersion={}

requests_log = logging.getLogger("requests")
requests_log.setLevel(logging.WARNING)

##File watching setup
path_to_watch = "/dev/disk/by-label/"
path_mount_base = "/tmp/fabhive"
filename_to_look_for="/config.ini"
before = dict ([(f, None) for f in os.listdir (path_to_watch)])

##Used for uploading files
register_openers()

if (hasLCD):
	import Adafruit_CharLCD as LCD
	# Initialize the LCD using the pins
	lcd = LCD.Adafruit_CharLCDPlate()
	lcd.set_color(1.0, 1.0, 0.0)
	lcd.clear()
	lcd.message('Starting...')
	time.sleep(3.0)



MINUTES = 60.0

# script filename (usually with path)
# print inspect.getfile(inspect.currentframe())
# script directory
script_directory=os.path.dirname(os.path.abspath(inspect.getfile(inspect.currentframe())))


# print script_directory
print "WorkerBee started."



def rebootscript():
    print "rebooting system"
    command = "/sbin/reboot"
    subprocess.call(command, shell = True)

def checkConfigFile():
	app_log.debug("Checking for new config file")
	global before
	after = dict ([(f, None) for f in os.listdir (path_to_watch)])
	added = [f for f in after if not f in before]
	removed = [f for f in before if not f in after]
	if added:
		i=0
		for f in added:
			app_log.debug("New Device Found: " + f)
			if not (os.path.isdir(path_mount_base + str(i))):
				os.mkdir(path_mount_base + str(i))
			subprocess.check_call(["mount",path_to_watch + f,path_mount_base + str(i)])
			if(os.path.isfile(path_mount_base+str(i)+filename_to_look_for)):
				app_log.debug("Found new config file")
				shutil.copyfile(path_mount_base+str(i)+filename_to_look_for,script_directory+filename_to_look_for);
				rebootscript()
			else:
				app_log.debug("No config file on drive, unmounting")
				subprocess.check_call(["umount",path_mount_base + str(i)])
			i=i+1
	else:
		app_log.debug("No new devices")

	if removed:
		app_log.debug("Removed: " + ' '.join(['%s' % f for f in removed]))
	before = after

def getOctoprintAPIVersion():
	global octoprintAPIVersion

	headers={'X-Api-Key':octoprint_api_key}
	try:
		r=requests.get('http://localhost:5000/' + 'api/version',headers=headers)
		decodedData=json.loads(r.text)
		octoprintAPIVersion['api']=decodedData['api']
		octoprintAPIVersion['server']=decodedData['server']
		app_log.debug("Octoprint API Versions: API(" + octoprintAPIVersion['api'] + ") Server("+octoprintAPIVersion['server']+")")
	except:
		app_log.debug("Exceptiong determining API version" + sys.exc_info()[0])
		app_log.debug("\tResponse Text: " + r.text)
		if(r.text == "Invalid API key"):
			octoprintAPIVersion['api']='9999'



def printerStatus():
	# data={'status':str(statusCode),'message':message}
	global isPrinting
	headers={'Authorization':api_key}
	try:
		r=requests.get(katana_url + 'bots/' + str(workerBeeId) ,headers=headers)
		bot_stats=json.loads(r.text)

		headers={'X-Api-Key':octoprint_api_key}
		r=requests.get('http://localhost:5000/' + 'api/job',headers=headers)
		decodedData=json.loads(r.text)
		if(decodedData['state']=='Offline'):
			updateBotStatus(3,'Printer is offline for octoprint')
			return 'offline'
		if ( decodedData['state'] == 'Operational' and bot_stats['status']==0):
			isPrinting=False
			return 'idle'
		if ( decodedData['state'] == 'Operational' and bot_stats['status']==1):
			isPrinting=False
			return 'printing complete'
		if ( decodedData['state'] == 'Printing' and bot_stats['status']!=0):
			return 'printing'
		if(decodedData['state'] == 'Printing' and bot_stats['status']==0):
			updateBotStatus(statusCode=1,message='Connected to the hive.')
			return 'printing'
		if ( decodedData['state'] == 'Closed' or bot_stats['status']!=0):
			return 'offline'
		return 'other'
	except:
		app_log.debug("Exceptiong determining printer status")
		app_log.debug("Response: " + r.text)
		app_log.debug("API Version: " + str(getOctoprintAPIVersion()))
		if(octoprintAPIVersion['api']=='9999'):
			app_log.debug("Bad API key for OctoPrint")
			updateBotStatus(3,'Bad API key for OctoPrint')
			return 'offline'
		else:
			return 'other'


def getPrintingStatus():
	global isPrinting
	headers={'X-Api-Key':octoprint_api_key}
	r=requests.get('http://localhost:5000/' + 'api/job',headers=headers)
	decodedData=json.loads(r.text)
	global printingStatus
	if ( decodedData['state'] == 'Printing'):
		printingStatus['percentComplete']=decodedData['progress']['completion']
		printingStatus['timeLeft']=decodedData['progress']['printTimeLeft']
		printingStatus['fileName']=decodedData['job']['file']['name']
		isPrinting=True
	else:
		printingStatus['percentComplete']=decodedData['progress']['completion']
		printingStatus['timeLeft']='0'
		printingStatus['fileName']='0'

	return printingStatus


def printerTemps():
	headers={'X-Api-Key':octoprint_api_key}
	r=requests.get('http://localhost:5000/' + 'api/printer',headers=headers)
	decodedData=json.loads(r.text)
	temps={}
	temps['bed']=decodedData['temps']['bed']['actual']
	temps['hotend']=decodedData['temps']['tool0']['actual']
	app_log.debug("bed: " + str(temps['bed']))
	app_log.debug("hotend: " + str(temps['hotend']))
	return temps

def updateLCD(message,color):
	if (hasLCD):
		lcd.clear()
		lcd.set_color(1.0, 1.0, 0.0)
		lcd.message(message)

def showIP():
	if (hasLCD):
		lcd.clear()
		# lcd.set_color(1.0,1.0,0.0)
		lcd.message("IP:" + [(s.connect(('8.8.8.8', 80)), s.getsockname()[0], s.close()) for s in [socket.socket(socket.AF_INET, socket.SOCK_DGRAM)]][0][1] + "\n")

def showStatus():
	status=printerStatus()

	if (hasLCD):
		lcd.clear()
		lcd.message("Printer Status: \n" + status)

	app_log.debug("Status: " + status)

def runCommand(gcode):
	app_log.debug('Running Command: ' + gcode)
	try:
		time.sleep(5)
		p.default(gcode)
		data={'command':'NULL'}
		headers={'Authorization':api_key}
		r=requests.put(katana_url + 'bots/' + str(workerBeeId) + '/command/',data=data,headers=headers)
		app_log.debug("Result: ")
		app_log.debug(r.text)
	except:
	 	e = sys.exc_info()[0]
	 	app_log.debug('Failed to connect to printer: %s' % e)

def markJobTaken(jobID):
	##Make sure job isn't already taken
	try:
		headers={'Authorization':api_key}
		r=requests.get(katana_url + 'jobs/' + str(jobID),headers=headers)
	except:
		return False

	decodedData=json.loads(r.text)
	if( decodedData['error']==True or decodedData['status']!=0):
		return False
	else:
		headers={'Authorization':api_key}
		data={'status':'1','bot':workerBeeId}
		try:
			r=requests.put(katana_url + 'jobs/' + str(jobID),data=data,headers=headers)
			decodedData=json.loads(r.text)
			if(decodedData['error']==False):
				app_log.debug("Mark Job Taken: " + r.text)
				return True
		except:
			return False

def markJobCompleted(jobID):
	app_log.debug("Marki Job Complete function for job id: " + str(jobID))
	if(jobID>0):
		headers={'Authorization':api_key}
		data={'status':'2','bot':workerBeeId}
		try:
			file = open('webcam.jpg','wb')
			file.write(urllib.urlopen("http://127.0.0.1:8080/?action=snapshot").read())
			file.close
			app_log.debug("Saved Image")
			im=Image.open('webcam.jpg')
			app_log.debug("Opened Image")
			if shouldFlipCamera:
				rotateImaged=im.rotate(180)
				app_log.debug("Rotated Image")
				rotateImaged.save('webcam-flipped.jpg')
				app_log.debug("Saved Rotated Image")
				file=open('webcam-flipped.jpg','r')
			else:
				file=open('webcam.jpg','r')

			files={'file':('webcam.jpg',file)}
		except:
			app_log.debug("Failed to get image of completed job: " + str(sys.exc_info()[0]))

		try:
			if 'files' in locals():
				app_log.debug("Posting Job Complete w/ Image: " + str(jobID))
				r=requests.post(katana_url + 'jobs/' + str(jobID),data=data,headers=headers,files=files)

			else:
				app_log.debug("Putting Job Complete w/out image: " + str(jobID))
				r=requests.put(katana_url + 'jobs/' + str(jobID),data=data,headers=headers)
			decodedData=json.loads(r.text)
			if(decodedData['error']==False):
				app_log.debug("Mark Job Completed: " + r.text)
				return True
			else:
				return True
		except:
			app_log.debug("Failed to mark job completed: " + str(jobID))
			return False

	return True

def addJobToOctoprint(job):
	##Download file
	app_log.debug("Downloading file: " + job['gcodePath'])
	try:
		r=requests.get(job['gcodePath'],stream=True)
		with open(job['gcodePath'].split('/')[-1], 'wb') as f:
			for chunk in r.iter_content(chunk_size=1024):
				if chunk: # filter out keep-alive new chunks
					f.write(chunk)
					f.flush()

		app_log.debug("Sending file to octoprint: " + job['gcodePath'])



		datagen, headers = multipart_encode({"file": open(job['gcodePath'].split('/')[-1], "rb")})
		headers['X-Api-Key']=octoprint_api_key
		request = urllib2.Request("http://localhost:5000/api/files/local", datagen, headers)
		# Actually do the request, and get the response
		print urllib2.urlopen(request).read()


		files = {job['gcodePath']: open(job['gcodePath'].split('/')[-1], 'rb')}
		r=requests.post( 'http://localhost:5000/api/files/local', headers=headers,files=files)
		# app_log.debug("Sent file to octoprint: " + r.text)
		# print "Response: " + str(r)
		# print "Response Text: " + str(r.text)
		decodedData=json.loads(r.text)
		if( decodedData['done']==True):
			os.remove(job['gcodePath'].split('/')[-1])
			return True
		else:
			return False
	except urllib2.URLError as e:
		print e.code
		print e.reason
		app_log.debug("Exception sending file to octoprint: "  + str(sys.exc_info()[0]) )
		return False

def octoprintFile(job):
	fileName=job['gcodePath'].split('/')[-1]
	headers={'X-Api-Key':octoprint_api_key,'Content-Type':'application/json'}
	# data={"command":"select"}
	data={"command":"select","print":"true"}
	# print "filename: " + fileName
	# print "Data: " + str(data)
	r=requests.post( 'http://localhost:5000/api/files/local/' + fileName, headers=headers, data=json.dumps(data))
	# print "Response: " + str(r.status_code)
	if(r.status_code==204):
		app_log.debug("Success")
		return True
	else:
		app_log.debug("Failed to print: " + str(r) + r.text)
		return False

def updateBotStatus(statusCode=99,message=''):
	app_log.debug("Updating printer status: " + message)
	if statusCode==99:
		data={'message':message}
		headers={'Authorization':api_key}
		try:
			r=requests.put(katana_url + 'bots/' + str(workerBeeId) + '/message',data=data,headers=headers)
		except:
			app_log.debug("Could not update bot status. Network Issue.")
	else:
		data={'status':str(statusCode),'message':message}
		headers={'Authorization':api_key}
		try:
			r=requests.put(katana_url + 'bots/' + str(workerBeeId),data=data,headers=headers)
		except:
			app_log.debug("Could not update bot status. Network Issue.")
		# print "response: " + r.text

#Twisted Implementation
class HiveClient(Protocol):
	def __init__(self, factory):
		self.factory = factory
		self.hasConnected=False
		self.checkInRepeater = LoopingCall(self.checkBotIn)
		self.configFileRepeater=LoopingCall(checkConfigFile)

	def connectionMade(self):
		data={'type':'connect','bot':workerBeeId}
		self.transport.write(json.dumps(data))

		updateBotStatus(statusCode=1,message='Connected to the hive.')
		##Check In to FabHive every minute
		self.checkInRepeater.start(1 * MINUTES)

		##Check for new config file every 30 seconds
		self.configFileRepeater.start(1 * .5 * MINUTES,now=True)
		self.hasConnected=True

	def dataReceived(self, data):
		global currentJobId
		app_log.debug( "> Received: ''%s''\n" % (data))
		messages=data.split('\n')

		for message in messages:
			app_log.debug("messages: " + message)
			decodedData=json.loads(message)
			if(decodedData['type']=='job'):
				app_log.debug("received a new job")
				updateBotStatus(statusCode=1,message='Received job: ' + decodedData['filename'])
				if(addJobToOctoprint(decodedData)==True):
					app_log.debug("This worked, mark the file as taken")
					result=markJobTaken(decodedData['id'])
					if(result==True):
						updateBotStatus(statusCode=1,message='Printing: ' + decodedData['filename'])
						currentJobId=decodedData['id']
						result=octoprintFile(decodedData)
					else:
						updateBotStatus(statusCode=0,message='Job was already taken')
						currentJobId=0
				else:
					updateBotStatus(statusCode=0,message='Job failed to load on Octoprint')
					currentJobId=0


	def stopAllTimers(self):
		app_log.debug("Stopping all timers")
		self.checkInRepeater.stop


	def checkBotIn(self):
		global printingStatus
		global isPrinting
		global currentJobId
		if(self.hasConnected):
			showStatus()
			app_log.debug("I should check in now. Queen Bee might be worried about me.")

			data={'type':'checkIn','bot':workerBeeId}
			self.transport.write(json.dumps(data) + '\n')

			status=printerStatus()

			app_log.debug("Status: " + status)
			app_log.debug("isPrinting: " + str(isPrinting))

			if(status=="printing complete"):
				printStatus=getPrintingStatus()
				if(currentJobId>0):
					if(printingStatus['percentComplete']==100):
						while True:
							app_log.debug("Marking job complete")
							result=markJobCompleted(currentJobId)
							app_log.debug("Marking job complete: " + str(result))
							if(result):
								app_log.debug("Job marked complete")
								break
						currentJobId=0

			if(status=="printing"):
				app_log.debug("I'm printing")
				printStatus=getPrintingStatus()
				updateBotStatus(statusCode=1,message='Printing: ' + printStatus['fileName'] + '<BR/>Percent Complete: ' + str(math.ceil(printStatus['percentComplete'])))

		 	if(status=="idle" and isPrinting==False):
				app_log.debug("Requesting job")
				self.requestJob()

		else:
			app_log.debug("We haven't connected yet. No need to check in yet.")

	def requestJob(self):
		if(self.hasConnected):
			data={'type':'jobRequest','bot':workerBeeId}
			self.transport.write(json.dumps(data))
		else:
			app_log.debug("We haven't connected yet.")


class WorkerBee(object):
	def __init__(self):
		updateBotStatus(statusCode=1,message='Printer is online.')
		if (hasLCD):
			lcd.set_color(0.0, 1.0, 0.0)
			lcd.clear()
			lcd.message('Connected.')




class HiveFactory(ReconnectingClientFactory):
	def __init__(self):
		self.protocol=HiveClient(self)
		self.checkTempRepeater = LoopingCall(self.checkPrinterTemp)
		self.workerBee=WorkerBee()
		self.checkTempRepeater.start(1*15)

	def startedConnecting(self, connector):
		app_log.debug('Started to connect.')


	def buildProtocol(self, addr):
		app_log.debug('Connected.')
		app_log.debug('Resetting reconnection delay')
		self.resetDelay()
		return HiveClient(self)

	def clientConnectionLost(self, connector, reason):
		app_log.debug('Lost connection.  Reason:' + str(reason))
		self.protocol.stopAllTimers();
		ReconnectingClientFactory.clientConnectionLost(self, connector, reason)

	def clientConnectionFailed(self, connector, reason):
		app_log.debug('Connection failed. Reason:' + str(reason))
		self.protocol.stopAllTimers();
		ReconnectingClientFactory.clientConnectionFailed(self, connector, reason)

	def checkPrinterTemp(self):
		# extruderTemp=self.workerBee.pronsole.status.extruder_temp
		if (hasLCD):
			temps=printerTemps()
			if(temps['hotend']>40):
				lcd.set_color(1.0,0.0,0.0)
			else:
				lcd.set_color(0.0,0.0,1.0)

			lcd.clear()
			lcd.message("E Temp:" + str(temps['hotend']) + "\n")
			lcd.message("B Temp:" + str(temps['bed']) + "\n")

getOctoprintAPIVersion()
reactor.connectTCP("fabhive.buzz", 5005, HiveFactory())

# reactor.callWhenRunning(WorkerBee())
reactor.run()

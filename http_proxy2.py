#!/usr/bin/python

# This HTTP proxy handles a single connection at once (multiple connections sequentially).
# Things that are missing:
# - pipelining
# - error handling for most errors
# - persistent connections to the server
# - writing a logfile with proper formatting
# - multiple connections in parallel
# - caching
# - making it fast
# - ...

import sys
from socket import *
import re
from mimetools import Message
from StringIO import StringIO
import datetime
import select
import signal
import struct
import hashlib
import thread
import threading

# timeout for connections
# if there is no message on a socket for this period of time, we close the socket
TIMEOUT=30

# define some special exception that we will throw in places
class TimeoutException(Exception):
	pass

# The following class is taken from bjorn12 and kari13's solution
# and slightly adapted.
#
# A wrapper class for sockets.
# This class implements the read() and readline() functions
# which provide an easy way to read HTTP messages from sockets.
# It also raises an exception on recv timeout.
class Socket:
	def __init__(self, socket):
		self.socket = socket
		# The buffer contains data that has been read
		# from the socket but not returned to the caller.
		self.buffer = ''

	# Closes the socket and empties the buffer.
	def close(self):
		self.buffer = ''
		self.socket.close()

	# Sends the data to the wrapped socket.
	def send(self, data):
		self.socket.send(data)
	
	# send all given data to the wrapped socket
	def sendAll(self, data):
		while len(data)>0:
			bytesSend = self.socket.send(data)
			data = data[bytesSend:]

	# Receives bytes from the socket via the recv function.
	# This function raises an exception on timeout.
	def recv(self, n):
		ready = select.select([self.socket], [], [], TIMEOUT)[0]
		if not ready:
			# Raise exception on timeout.
			raise TimeoutException('reading takes too long')
		return self.socket.recv(n)


	# Read at most n bytes from the socket.
	def read(self, n):
		data = ''
		if self.buffer == '':
			# Read directly from the socket.
			data = self.recv(n)
		else:
			# Read from the buffer.
			data = self.buffer[:n]
			self.buffer = self.buffer[n:]
			#else:
				## Read the buffer and then the remaining bytes from the socket.
				#data = self.buffer
				#n = n - len(self.buffer)
				#self.buffer = ''
				#data = data + self.recv(n)
		return data

	# Read a line from the socket.
	# This function defines a line as a string that ends with CRLF.
	def readline(self):
		length = 256
		index = self.buffer.find('\r\n')
		while index == -1:
			data = self.recv(length)
			# Return the empty message if received.
			if data == '':
				return ''
			self.buffer = self.buffer + data
			index = self.buffer.find('\r\n')
		# +2 to include the CRLF
		line = self.buffer[:index + 2]
		self.buffer = self.buffer[index + 2:]
		return line


	# read exactly the number of bytes given (not less)
	def readExactNumBytes(self, numBytes):
		data = self.read(numBytes)
		while len(data) < numBytes:
			data += self.read(numBytes - len(data))
		return data

# handle one connection from a client
# return when the connection is closed or when a timeout occurs
def handleConnection(clientSocket, clientAddr):
	serverSocket = None
	# wrap the socket, so we can use readline()
	clientSocket = Socket(clientSocket)
	# keep the connection alive until keepAlive turns false (for whatever reason)
	keepAlive = True
	while keepAlive:
		try:
			# TODO: handle pipelining
			request = readRequestHeader(clientSocket)
			# handle the request
			if request != None:
				#print request.method, request.hostname, request.port, request.path, request.version
				#print str(request.headers)
				# open connection to server
				serverSocket = Socket(socketToServer(request.hostname, request.port))
				# forward request
				forwardMessage(request, clientSocket, serverSocket, None)
				# read response from server
				response = readResponseHeader(serverSocket)
				lock.acquire()
				logMessage(clientAddr, request, response.statusCode, response.statusMessage)
				lock.release()
				# send response to client
				if request.method == 'GET':
					forwardMessage(response, serverSocket, clientSocket, request)
				else:
					forwardMessage(response, serverSocket, clientSocket, None)
				serverSocket.close()
				serverSocket = None
				#print "-----"
				# TODO: reuse connection to server for more requests
				connectionHeader = request.headers.getheader('Connection')
				# close connection to client if client does not want to keep it open
				if request.version == 'HTTP/1.0':
					keepAlive = connectionHeader != None and connectionHeader.lower() == 'keep-alive'
				else: # HTTP/1.1
					keepAlive = connectionHeader == None or connectionHeader.lower() == 'keep-alive'
			else: # connection was closed by client
				keepAlive = False
		except TimeoutException, e:
			print "timeout occured, closing connection to client"
			keepAlive = False
		#except socket.error as e:
			#print "ERROR on connection to " + str(clientHost) + ":" + str(clientPort) + " :" + str(e)
			#print "connection closed after error: " +  str(clientHost) + ":" + str(clientPort)
			#keepAlive = False
	
	# close all sockets opened for that client
	print "closing connection to client"
	if serverSocket != None:
		serverSocket.close()
	clientSocket.close()
## end of handleConnection

# connect to a server and return the socket
def socketToServer(hostName, port):
	#print "connecting to " + hostName + ":" + str(port)
	host = gethostbyname(hostName)
	connectionSocket = socket(AF_INET, SOCK_STREAM)
	connectionSocket.connect((host, port))
	#print "connected"
	return connectionSocket
	
def logMessage(clientAddr, request, statusCode, statusMessage):
	# TODO: write log to logfile in proper format
	dt = datetime.datetime.now()
    	dt = dt.isoformat()
    	dt = str(dt[:-7]) + "+" + str(dt[-6:-2])
	#print str(dt) + " : " + str(clientAddr[0]) + ":" + str(request.port) + " : " + request.method + " http://" + request.hostname + "/" + request.path + " : " + str(statusCode) + " " + str(statusMessage.replace('\r\n',''))
	# 2014-10-07T18:00:51+0000 : 127.0.0.1:36122 GET http://www.google.com/ : 200 Ok
	#print request.method, request.hostname, request.port, request.path, request.version
	with open(logfileName, 'a') as file:
		file.write(str(dt) + " : " + str(clientAddr[0]) + ":" + str(request.port) + " : " + request.method + " http://" + request.hostname + "/" + request.path + " : " + str(statusCode) + " " + str(statusMessage.replace('\r\n','')) + "\n")
		file.close()

def writeToCache(message, fileName):
	with open(fileName, 'a') as file:
		file.write(message)
		file.close()

def getFileName(request, language):
	return hash(request.hostname + request.path + language)

# forward an HTTP message from source file (allows us to use readline()) to
# a destination socket without reading the whole message into memory first
def forwardMessage(messageHeader, sourceSocket, destSocket, request): 
	# add a via header to tell the receiver that we are a proxy
	messageHeader.headers['Via'] = 'SomeProxy'
	# send the header (may include request line or response code line)
	destSocket.send(str(messageHeader))
	# check whether a message is chunked or not by looking at
	# Transfer-Encoding and/or Content-Length header
	transferEncoding = messageHeader.headers.getheader('Transfer-Encoding')
	contentLengthString = messageHeader.headers.getheader('Content-Length')
	chunked = False
	contentLength = 0
	if request != None:
		cacheData = str(messageHeader)
	if transferEncoding != None and transferEncoding.lower().endswith('chunked'):
		chunked = True
	elif contentLengthString != None:
		contentLength = int(contentLengthString)
	if chunked:
		line = sourceSocket.readline()
		destSocket.sendAll(line)
		chunkSize = int(line, 16)
		while chunkSize > 0:
			data = sourceSocket.readExactNumBytes(chunkSize + 2)
			if request != None:
				cacheData = cacheData + str(data) ### Andri: safna data til ad cacha 
			destSocket.sendAll(data)
			line = sourceSocket.readline()
			destSocket.sendAll(line)
			chunkSize = int(line, 16)
		# after the chunkSize = 0 line, there is an empty chunk, read and send that
		line = sourceSocket.readline()
		destSocket.sendAll(line)
	elif contentLength > 0:
		bufferSize = 4096
		bytesRead = 0
		while bytesRead < contentLength:
			# read missing data, but at most bufferSize bytes
			data = sourceSocket.read(min(bufferSize, contentLength - bytesRead))
			if request != None:
				cacheData = cacheData + str(data) ### Andri: safna data til ad cacha 
			bytesRead += len(data)
			destSocket.sendAll(data)
	#print messageHeader
	if request != None:
		print str(cacheData)
		cacheItems[request.hostname + request.path] = {'created': datetime.datetime.now(), 'data': cacheData} ## Andri: tharf ad breyta dateTime now.

## end forwardMessage

class RequestHeader:
	def __init__(self):
		self.method = ''
		self.hostname = '' 
		self.port = 80
		self.path = '/'
		self.version = ''
		self.headers = {}

	# returns the request header as it should be send to the web server
	def __str__(self):
		strings = [
			self.method, ' ', self.path, ' ', self.version, '\r\n'
			]
		strings.append(str(self.headers))
		strings.append('\r\n')
		return ''.join(strings)
## end class RequestHeader

class ResponseHeader:
	def __init__(self):
		self.version = ''
		self.statusCode = 200
		self.statusLine = 'OK'
		self.headers = {}

	# returns the request header as it should be send to the web server
	def __str__(self):
		strings = [
			self.version, ' ', self.statusCode, ' ', self.statusLine, '\r\n'
			]
		strings.append(str(self.headers))
		strings.append('\r\n')
		return ''.join(strings)
## end class ResponseHeader

def readHTTPHeaders(socket):
	# read lines until the end of the header is reached
	headerLines = []
	line = socket.readline()
	while line != '\r\n' and line != '':
		headerLines += line
		line = socket.readline()
	if line == '':
		raise Exception('Malformed Message')
	# read the headers
	return Message(StringIO(''.join(headerLines)))

# read the header of a request
def readRequestHeader(clientSocket):
	request = RequestHeader()
	requestLine = clientSocket.readline()
	if requestLine == '':
		return None

	request.method, URI, request.version = requestLine.split()

	request.method = request.method.upper()
	supportedMethods = ['GET', 'HEAD', 'POST']
	if not request.method in supportedMethods:
		raise Exception('Unsupported Method')

	# parse the URI
	# request.hostname, request.port, request.path = parseURI(URI)
	match = re.match('http://([^/:]*)(:[0-9]*)?(/.*)?',URI)
	if match == None:
		raise Exception('Bad Request')
	request.hostname = match.group(1)
	request.port = match.group(2)
	request.path = match.group(3)
	# default port is 80, if no port is given
	if request.port ==  None:
		request.port = 80
	else:
		request.port = int(request.port)
	# default path is /, if no path is given
	if request.path == None:
		request.path = '/'
	
	# read the headers
	request.headers = readHTTPHeaders(clientSocket)
	return request

# read the header of a response
def readResponseHeader(serverSocket):
	response = ResponseHeader()
	statusLine = serverSocket.readline()
	if statusLine == '':
		return None

	response.version, response.statusCode, response.statusMessage = statusLine.split(' ', 2)

	# read the headers
	response.headers = readHTTPHeaders(serverSocket)
	return response

logfileName = ""
lock = threading.Lock()
cacheItems = {}

def main():
	global logfileName, lock, cacheItems
	# read command line arguments
	if len(sys.argv) != 3:
		print "USAGE: python http_proxy.py PORT LOGFILE"
		sys.exit(-1)
	
	proxyPort = int(sys.argv[1])
	logfileName = sys.argv[2]
 	open(logfileName, 'a').close()
	# create server socket for the proxy to listen on
	proxySocket = socket(AF_INET, SOCK_STREAM)
	# next line allows reusing the port immediately
	proxySocket.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)
	proxySocket.bind(('', proxyPort))
	proxySocket.listen(0)
	while 1:
		# wait for incoming connection
		clientSocket, clientAddr = proxySocket.accept()
		# handle the connection
		# TODO: for part 2 we need to be able to handle several incoming connections in parallel
		thread.start_new_thread(handleConnection, (clientSocket, clientAddr))

if __name__ == '__main__':
	main()


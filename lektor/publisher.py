import ftplib
import posixpath
from inifile import IniFile
from lektor.builder import FileInfo
from lektor.db import to_posix_path
from lektor.exceptions import FTPException, FileNotFound, \
                              ConnectionFailed, IncorrectLogin, \
                              TVFSNotSupported, RootNotFound

import time
import os

class FTPConnection(object):
    '''Currently assumes that the server has TVFS'''
    #TODO Fallback from TVFS
    def __init__(self, server):
        self._server = server
        self._ftp = None
        #self._tvfs = False
    
    def __del__(self):
        if self._ftp:
            try:
                self._ftp.quit()
            except Exception:
                pass
    
    def _connect(self):
        #TODO if not default port, TLS, etc
        try:
            host = ftplib.FTP(self._server['host'])
        except ftplib.all_errors:
            # Mostly socket.gaierror(Errno 11004) and socket.error (10060)
            raise ConnectionFailed('Connection to host failed!')
        try:
            host.login(self._server['user'], self._server['pw'])
        except ftplib.error_perm:
            raise IncorrectLogin('Login or password incorrect!')
        try:
            #TODO set _tvfs
            if 'tvfs' not in host.sendcmd('FEAT').lower():
                raise TVFSNotSupported('Host does not support TVFS!')
        except ftplib.all_errors:
            raise FTPException
        try:
            host.cwd(self._server['root'])
        except ftplib.error_perm as e:
            if e.message.startswith('550 Can\'t change directory'):
                raise RootNotFound('Root directory \"' \
                                    + self._server['root'] + '\" not found!')
            raise FTPException(e.message)
        return host
    
    def _connection(self):
    
        if self._ftp is None:
            self._ftp = self._connect()
        else:
            try:
                self._ftp.voidcmd('NOOP')
                return self._ftp
            except ftplib.all_errors as e:
                print 'Host timeout, reconnecting...'
                self._ftp = self._connect()
        return self._ftp
    
    def retrbinary(self, filename):
        file = None
        try:
            file = self._connection().retrbinary('RETR ' + filename, None)
        except ftplib.error_perm as e:
            if e.message.startswith('550 Can\'t open'):
                raise FileNotFound('File \"' + filename + '\" not found!')
            raise FTPException(e.message)
        return file
            
class FTPHost(object):

    def __init__(self, server):
        self._server = server
        self._con = FTPConnection(server)
        
    def __del__(self):
        del self._con
        
    def get_file(self, filename):
        file = self._con.retrbinary(filename)
        
    def put_file(self, src, dst):
        return dst


class Publisher(object):
    
    def __init__(self, src, srv_name, force=False):
        self._src = src
        self._force = force
        self._server = {}
        i = IniFile(os.path.join(os.path.dirname(src), 'publish.ini')).to_dict()
        self._server['host'] = i[srv_name+'.host']
        self._server['port'] = i[srv_name+'.port']
        self._server['user'] = i[srv_name+'.user']
        self._server['pw']   = i[srv_name+'.pw']
        self._server['root']   = i[srv_name+'.root']
        self._artifacts = {}
    
    '''def _decode_artifacts_file(self, file):
        f = gzip.GzipFile(fileobj=file)
            for line in f:
                line = line.decode('utf-8').strip().split('\t')
                self._artifacts[line[0]] = FileInfo(
                    build_state.env,
                    filename=line[0],
                    mtime=int(line[1]),
                    size=int(line[2]),
                    checksum=line[3],
                )'''
    '''def update(self, iterable):
        changed = False
        old_artifacts = set(self.artifacts)

        for artifact_name, info in iterable:
            old_info = self.artifacts.get(artifact_name)
            if old_info != info:
                self.artifacts[artifact_name] = info
                changed = True
            old_artifacts.discard(artifact_name)

        if old_artifacts:
            changed = True
            for artifact_name in old_artifacts:
                self.artifacts.pop(artifact_name, None)

        return changed'''
    
    def _get_remote_artifacts_file(self):
        '''Returns the artifacts file or None if not found.'''
        ftp = FTPHost(self._server)
        try:
            return ftp.get_file('.lektor/artifacts.gz')
        except FileNotFound:
            return None
        
    def calculate_change_list(self):
        #TODO handling if artifacts.gz was not found 
        #-> Root dir manipulated?
        #-> Fresh directory / Initial sync?
        f = self._get_remote_artifacts_file()
        if f:
            f = gzip.GzipFile(fileobj=f)
            for line in f:
                line = line.decode('utf-8').strip().split('\t')
                self._artifacts[line[0]] = FileInfo(
                    build_state.env,
                    filename=build_state.get_destination_filename(line[0]),
                    mtime=int(line[1]),
                    size=int(line[2]),
                    checksum=line[3],
                )

    def publish(self):
        try:
            change_list = self.calculate_change_list()
        except FTPException as e:
            print e
            exit()
        
        
        #print con.ftp.pwd()
        #time.sleep(5)
        #print con.ftp.pwd()
        #time.sleep(12)
        #print con.ftp.pwd()
        #self.ftp.connect()
        #self.ftp.chdir(self.destination)
        #TODO if destination is empty:
        #self.initial_upload()
        print "Publish finished without errors."
            


'''def get_ftp_root(self, root):
        ftp_root = os.path.join(*root.split(self.source)[1::])
        #remove preceding slash
        ftp_root = ftp_root.replace(os.path.sep, '', 1)
        ftp_root = ftp_root.replace(os.path.sep, self.ftp.host.sep)
        # XXX: ftp_root is now unicode!
        ftp_root = self.ftp.host.path.join(self.destination, ftp_root)
        return ftp_root
    
    def initial_upload(self):
        try:
            orig_root = self.source
            for root, dirs, files in os.walk(self.source):
                ftp_root = self.get_ftp_root(root)
                # XXX: use self.chdir
                self.ftp.host.chdir(ftp_root)
                
                for dir in dirs:
                    # XXX: use self.mkdir
                    self.ftp.host.mkdir(dir)
                    
                for file in files:
                    src = os.path.join(root, file)
                    tgt = self.ftp.host.path.join(ftp_root, unicode(file))
                    # XXX: use self.upload
                    print "Uploading: " + file
                    self.ftp.host.upload(src, tgt)
        except FTPError as e:
            print e.strerror
            # XXX: raise error
            exit()'''



'''
class FtpConnection(object):

    def __init__(self, server_url, user, pw)
        self._server_url = server_url
        self._user = user
        self._pw = pw
        self._ftp = None
        
    @property
    def ftp(self):
        if self.ftp is None:
            self._ftp = FTP(self._server_url, self._user, self._pw)
        return self._ftp
        
    def connect(self):
        self._ftp = FTP(self._server_url, self._user, self._pw)
    
        
            
            
server_url = "127.0.0.1"
user = "Tester"
pw = "tester"

wd = 'deleteTest_2_2_2'

ftp = FTP(server_url, user, pw)
ftp.cwd(wd)
root = FtpTree()
root.build_tree(ftp)

#delete root folder (with all folders and files in it)
for path, file in root.walk():
    ftp.delete(posixpath.join(path, file.name))
dirs = []
for path, dir in root.walk_dirs():
    dirs.append(posixpath.join(path, dir.name))    
for dir in dirs[::-1]:
    ftp.rmd(dir)
ftp.rmd(ftp.pwd())


    
######################################
#old
######################################


class Publisher():

    def __init__(self, root_path, remote_path):
        self.root_path = root_path
        self.remote_path = remote_path

def abspatha(*paths):
    filename = os.path.join(*(paths or ('',)))
    if not os.path.isabs(filename):
        filename = os.path.join(self.remote_path, filename)        
    return filename

root_dir = "/Client1"
    
def abspath(*paths):
    filepath = posixpath.join(*(paths or ('',)))
    if not posixpath.isabs(filepath):
        filepath = posixpath.join(root_dir, filepath)
    return filepath
        
def parse_remote_files(ftp, directory=''): 

    wd = abspath(directory)

    #TODO make function for this
    if ftp.pwd() != wd:
        ftp.cwd(wd)

    rdict = {}
    rlist = []
    ftp.retrlines('MLSD', rlist.append)

    #TODO look at every fact to determine what it is
    for f in rlist:
        unpack = f.split(';')
        type = unpack[0]
        data = unpack[1:]
        if type.startswith("type="):
            type = type.split('=')[1]
            if type == 'dir':
                folder = posixpath.join(wd, data[1].strip())
                #TODO directory dict to create or delete dicts before updating files later
                rdict[folder] = 'd'
                rdict.update(parse_remote_files(ftp, folder))
            elif type == 'file':
                rdict[posixpath.join(wd, data[2].strip())] = (data[0].split('=')[1], data[1].split('=')[1])
            else:
                continue #throw MLSD error
            
        else:
            continue #throw MLSD error
    return rdict

    
#TODO connect to absolute path of cwd and check it
#server_url = "127.0.0.1"
#root_dir = "/Client1"
#ftp = FTP(server_url, "Tester", "tester")

#rdict = parse_remote_files(ftp, root_dir)
    
#print rdict

#['type=file;modify=20150103222118;size=0; asdf.txt', 'type=dir;modify=20150103222126; testsasdf']

'''
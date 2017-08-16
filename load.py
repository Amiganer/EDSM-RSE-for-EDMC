"""
EDSM-RSE a plugin for EDMC
Copyright (C) 2017 Sebastian Bauer

This program is free software; you can redistribute it and/or
modify it under the terms of the GNU General Public License
as published by the Free Software Foundation; either version 2
of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program; if not, write to the Free Software
Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.
"""

import sys
import os
import math
import json
import re
import urllib2
import webbrowser
import sqlite3
import time

from threading import Thread
from Queue import Queue

import Tkinter as tk
import myNotebook as nb

from l10n import Locale
from config import config
import plug

if __debug__:
    from traceback import print_exc

VERSION = "0.1 Beta"
EDSM_UPDATE_INTERVAL = 3600 # 1 hour. used for EliteSystem
EDSM_NUMBER_OF_SYSTEMS_TO_QUERY = 25
DEFAULT_UPDATE_INTERVAL = 1
DEFAULT_RADIUS = 1000
# regex taken from EDTS https://bitbucket.org/Esvandiary/edts
PG_SYSTEM_REGEX = re.compile(r"^(?P<sector>[\w\s'.()/-]+) (?P<l1>[A-Za-z])(?P<l2>[A-Za-z])-(?P<l3>[A-Za-z]) (?P<mcode>[A-Za-z])(?:(?P<n1>\d+)-)?(?P<n2>\d+)$")
MC_VALUES = { "a" : 0, "b" : 1, "c" : 2, "d" : 3, "e" : 4, "f" : 5, "g" : 6, "h" : 7}

# keys for dictionary that stores data from the background thread
# stored in this.lastEventInfo
BG_SYSTEM = "bg_system"
BG_DISTANCE = "bg_distance"
BG_UNCERTAINTY = "bg_uncertainty" # 

this = sys.modules[__name__]	# For holding module globals

class EliteSystem(object):
    def __init__(self, id, name, x, y, z, updated_at = None):
        self.id = id
        self.name = name
        self.x = x
        self.y = y
        self.z = z
        self.updated_at = updated_at or 0
        self.distance = 10000 #set initial value to be out of reach

    def getUncertainty(self):
        if PG_SYSTEM_REGEX.match(self.name):
            mc = self.name.split(" ")[-1][:1].lower()
            return (10 * 2 ** MC_VALUES.get(mc, 0)) / 2
        return 0

    @staticmethod
    def calculateDistance(x1, x2, y1, y2, z1, z2):
        return math.sqrt((x1 - x2) ** 2 + (y1 - y2) ** 2 + (z1 - z2) ** 2)

    def updateDistanceToCurrentCommanderPosition(self, x, y, z):
        self.distance = self.calculateDistanceToCoordinates(x, y, z)

    def calculateDistanceToCoordinates(self, x2, y2, z2):
        return self.calculateDistance(self.x, x2, self.y, y2, self.z, z2)

    def calculateDistanceToSystem(self, system2):
        return self.calculateDistanceToCoordinates(system2.x, system2.y, system2.z)

    def __str__(self):
        return "id: {id}, name: {name}, distance: {distance:,.2f}, updated: {updated}, uncertainty: {uncertainty}".format(id=self.id, name=self.name, distance=self.distance, updated=self.updated_at, uncertainty=self.getUncertainty())

    def __repr__(self):
        return self.__str__()

    def __eq__(self, other):
        if isinstance(other, self.__class__):
            return self.id == other.id
        return False

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash(self.id)


class BackgroundWorker(Thread):
    
    # instructions
    JUMPED_SYSTEM = 0

    def __init__(self, queue, radius = DEFAULT_RADIUS, updateInterval = DEFAULT_UPDATE_INTERVAL):
        Thread.__init__(self)
        self.queue = queue
        self.radius = radius
        self.updateInterval = updateInterval
        self.counter = -1
        self.systemList = list()
        self.systemListHighUncertainty = list()
        self.systemDict = dict()

    def openDatabase(self):
        if not os.path.exists(os.path.join(os.path.dirname(__file__), "systemsWithoutCoordinates.sqlite")):
            plug.show_error("EDSM-RSE: Database could not be opened")
            sys.stderr.write("EDSM-RSE: Database could not be opened\n")
            return
        try:
            self.conn = sqlite3.connect(os.path.join(os.path.dirname(__file__), "systemsWithoutCoordinates.sqlite"))
            self.c = self.conn.cursor()
        except Exception as e:
            error = "Database could not be opened"

    def closeDatabase(self):
        if not hasattr(self, "c") or not self.c:
            return # database not loaded

        self.conn.close()

    def initializeDictionaries(self):
        if not hasattr(self, "c") or not self.c:
            return # database not loaded
        self.realNameToPg = dict()
        self.pgToRealName = dict()
        for row in self.c.execute("SELECT * FROM duplicates"):
            _, realName, pgName = row
            self.realNameToPg.setdefault(realName.lower(), list())
            self.realNameToPg.get(realName.lower(), list()).append(pgName)
            self.pgToRealName[pgName.lower()] = realName

    def generateListsFromDatabase(self, x, y, z):
        sql = "SELECT * FROM systems WHERE systems.x BETWEEN ? AND ? AND systems.y BETWEEN ? AND ? AND systems.z BETWEEN ? AND ?"
        # make sure that the between statements are BETWEEN lower limit AND higher limit
        systems = list()
        self.c.execute(sql, (x - self.radius, x + self.radius, y - self.radius, y + self.radius, z - self.radius, z + self.radius))
        for row in self.c.fetchall():
            _, _, x2, y2, z2, _ = row
            distance = EliteSystem.calculateDistance(x, x2, y, y2, z, z2)
            if distance <= self.radius:
                eliteSystem = EliteSystem(*row)
                eliteSystem.distance = distance
                systems.append(eliteSystem)
        systems.sort(key=lambda l: l.distance)

        self.systemList = systems
        self.systemDict = dict()
        for system in self.systemList:
            self.systemDict.setdefault(system.name.lower(), system)
        for system in self.systemListHighUncertainty:
            self.systemDict.setdefault(system.name.lower(), system)

    def updateTimeForSystems(self, systems, t):
        if __debug__: print("updateTimeForSystems for {} systems".format(len(systems)))
        for system in systems:
            system.updated_at = t
            self.c.execute("UPDATE systems SET last_checked = ? WHERE systems.id = ?", (t, system.id))
        if (systems):
            self.conn.commit() # commit only if the list contained items

    def removeSystemsFromDatabase(self, systems):
        for system in systems:
            self.c.execute("DELETE FROM systems WHERE systems.id = ?", (system.id,))
        self.conn.commit()

    def removeSystems(self, systems):
        if __debug__: print("removing {} systems".format(len(systems)))
        self.systemList = filter(lambda x: x not in systems, self.systemList)
        self.systemListHighUncertainty = filter(lambda x: x not in systems, self.systemListHighUncertainty)

        for system in systems:
            self.systemDict.pop(system.name.lower(), None)

    def queryEDSM(self, systems):
        """ returns a set of systems names in lower case with known coordinates """
        # TODO handle dupes
        edsmUrl = "https://www.edsm.net/api-v1/systems?onlyKnownCoordinates=1&"
        params = list()
        currentTime = int(time.time())
        systemsToUpdateTime = list()
        for system in systems:
            if (currentTime - system.updated_at) > EDSM_UPDATE_INTERVAL:
                params.append("systemName[]={name}".format(name=urllib2.quote(system.name)))
                systemsToUpdateTime.append(system)
        edsmUrl += "&".join(params)
        if __debug__: print("querying EDSM for {} systems".format(len(params)))
        if len(params) > 0:
            try:
                url = urllib2.urlopen(edsmUrl, timeout=5)
                response = url.read()
                edsmJson = json.loads(response)
                names = set()
                for entry in edsmJson:
                    names.add(entry["name"].lower())
                self.updateTimeForSystems(systemsToUpdateTime, currentTime)
                return names
            except:
               # ignore. the EDSM call is not required
               if __debug__: print_exc()
        return set()

    def handleJumpedSystem(self, coordinates, starName):
        if not hasattr(self, "c") or not self.c:
            return # no database. do nothing

        self.counter += 1
        tick = self.counter % self.updateInterval == 0
        if tick: 
            if __debug__: print("interval tick")
            # interval -> update systems
            self.generateListsFromDatabase(*coordinates)
            lowerLimit = 0
            upperLimit = EDSM_NUMBER_OF_SYSTEMS_TO_QUERY
            
            tries = 0
            while tries < 3: # no do-while loops...
                closestSystems = self.systemList[lowerLimit:upperLimit]
                currentTime = int(time.time())
                edsmResults = self.queryEDSM(closestSystems)
                if len(edsmResults) > 0:
                    # remove systems with coordinates
                    systemsWithCoordinates = filter(lambda s: s.name.lower() in edsmResults, closestSystems)
                    self.removeSystemsFromDatabase(systemsWithCoordinates)
                    self.removeSystems(systemsWithCoordinates)
                    closestSystems = filter(lambda s: s.name.lower() not in edsmResults, closestSystems)
                if len(closestSystems) > 0:
                    # there are still systems in the results -> stop here
                    break
                else:
                    tries += 1
                    lowerLimit += EDSM_NUMBER_OF_SYSTEMS_TO_QUERY
                    upperLimit += EDSM_NUMBER_OF_SYSTEMS_TO_QUERY

            if len(closestSystems) > 0:
                closestSystem = closestSystems[0]
                # 1.732051 is the length of the vector (1, 1, 1) and is the distance in the worst case
                if (closestSystem.getUncertainty() * 1.732051) > self.radius and closestSystem not in self.systemListHighUncertainty:
                    self.systemListHighUncertainty.append(closestSystem)
                print (closestSystem) # TODO
            else:
                pass # TODO remove UI elements

        if starName.lower() in self.systemDict: # arrived in system without coordinates
            # TODO handle dupes
            if __debug__: print("arrived in {}".format(starName))
            system = self.systemDict.get(starName.lower(), None)
            if system:
                self.removeSystemsFromDatabase([system])
                self.removeSystems([system])

            if not tick:
                # distances need to be recalculated
                for system in self.systemList:
                    system.updateDistanceToCurrentCommanderPosition(*coordinates)
                self.systemList.sort(key=lambda l: l.distance)
            print(self.systemList[0]) # TODO

    def run(self):
        self.openDatabase()
        self.initializeDictionaries()
        while True:
            instruction, args = self.queue.get()
            if not instruction:
                break

            if instruction == self.JUMPED_SYSTEM:
                self.handleJumpedSystem(*args)
            self.queue.task_done()
        self.closeDatabase()
        self.queue.task_done()

def checkTransmissionOptions():
    eddn = (config.getint("output") & config.OUT_SYS_EDDN) == config.OUT_SYS_EDDN
    edsm = config.getint('edsm_out') and 1
    return eddn or edsm

def plugin_start():
    this.queue = Queue()
    this.worker = BackgroundWorker(this.queue)
    this.worker.name = "EDSM-RSE Background Worker"
    this.worker.daemon = True
    this.worker.start()

    this.enabled = checkTransmissionOptions()

    return 'EDSM-RSE'

def updateUI():
    if not this.enabled or not this.lastEventInfo.get(BG_SYSTEM, None):
        this.unconfirmedText.grid_remove()
        this.unconfirmedSystem.grid_remove()
        this.distanceText.grid_remove()
        this.distanceValue.grid_remove()
        this.emptyFrame.grid(row = 0)
    else:
        this.emptyFrame.grid_remove()
        this.unconfirmedText.grid(row=0, column=0, sticky=tk.W)
        this.unconfirmedSystem.grid(row=0, column=1, sticky=tk.W)
        this.unconfirmedSystem["text"] = this.lastEventInfo.get(BG_SYSTEM, "")
        this.distanceText.grid(row=1, column=0, sticky=tk.W)
        this.distanceValue.grid(row=1, column=1, sticky=tk.W)
        this.distanceValue["text"] = u"{distance} Ly (\u00B1{uncertainty})".format(distance=Locale.stringFromNumber(this.lastEventInfo.get(BG_DISTANCE, 0)), uncertainty=this.lastEventInfo.get(BG_UNCERTAINTY, 0))

def plugin_close():
    # Signal thread to close and wait for it
    this.queue.put((None, None))
    this.worker.join()
    this.worker = None

def plugin_prefs(parent):
    frame = nb.Frame(parent)
    return frame

def prefs_changed():
    this.enabled = checkTransmissionOptions()

def plugin_app(parent):
    frame = tk.Frame(parent)
    frame.columnconfigure(1, weight=1)
    this.emptyFrame = tk.Frame(frame)
    this.unconfirmedText = tk.Label(frame, text="Unconfirmed:")
    this.unconfirmedSystem = tk.Label(frame)
    this.distanceText = tk.Label(frame, text="Distance:")
    this.distanceValue = tk.Label(frame)
    this.lastEventInfo = dict()
    updateUI()
    return frame

def journal_entry(cmdr, is_beta, system, station, entry, state):
    if not this.enabled or is_beta:
        return # nothing to do here
    if entry["event"] == "FSDJump" or entry["event"] == "Location":
        if "StarPos" in entry:
            this.queue.put((BackgroundWorker.JUMPED_SYSTEM, (tuple(entry["StarPos"]), entry["StarSystem"])))


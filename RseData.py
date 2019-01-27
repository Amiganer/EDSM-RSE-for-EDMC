"""
EDSM-RSE a plugin for EDMC
Copyright (C) 2019 Sebastian Bauer

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

import plug
import sys
import psycopg2

from EliteSystem import EliteSystem


class RseData:

    VERSION = "1.1"
    VERSION_CHECK_URL = "https://gist.githubusercontent.com/Thurion/35553c9562297162a86722a28c7565ab/raw/RSE_update_info"

    # settings for search radius
    DEFAULT_RADIUS_EXPONENT = 2  # key for radius, see calculateRadius
    MAX_RADIUS = 10
    RADIUS_ADJUSTMENT_INCREASE = 15  # increase radius if at most this amount of systems were found
    RADIUS_ADJUSTMENT_DECREASE = 100  # decrease the radius if at least this amount of systems were found

    EDSM_NUMBER_OF_SYSTEMS_TO_QUERY = 15

    # Values for projects
    PROJECT_RSE = 1
    PROJECT_NAVBEACON = 2

    # keys for dictionary that stores data from the background thread
    BG_SYSTEM = "bg_system"  # string
    BG_MESSAGE = "bg_message"  # string
    BG_JSON = "bg_json"  # if more information is needed: json object

    # name of events
    EVENT_RSE_UPDATE_AVAILABLE = "<<EDSM-RSE_UpdateAvailable>>"
    EVENT_RSE_BACKGROUNDWORKER = "<<EDSM-RSE_BackgroundWorker>>"

    def __init__(self, radiusExponent=DEFAULT_RADIUS_EXPONENT):
        self.newVersionInfo = None
        self.systemList = list()  # nearby systems, sorted by distance
        self.projectsDict = dict()
        self.frame = None
        self.filter = set()  # systems that have been completed
        self.lastEventInfo = dict()  # used to pass values to UI. don't assign a new value! use clear() instead
        self.radiusExponent = radiusExponent
        self.frame = None
        self.c = None
        self.conn = None

    def setFrame(self, frame):
        self.frame = frame

    def openDatabase(self):
        try:
            self.conn = psycopg2.connect(host="cyberlord.de", port=5432, dbname="edmc_rse_db", user="edmc_rse_user",
                                         password="asdfplkjiouw3875948zksmdxnf", application_name="EDSM-RSE {}".format(RseData.VERSION), connect_timeout=10)
            self.c = self.conn.cursor()
        except Exception as e:
            plug.show_error("EDSM-RSE: Database could not be opened")
            sys.stderr.write("EDSM-RSE: Database could not be opened\n")

    def closeDatabase(self):
        if not hasattr(self, "c") or not self.c:
            return  # database not loaded
        self.conn.close()
        self.c = None
        self.conn = None

    def isDatabaseAccessible(self):
        return hasattr(self, "c") and self.c

    def adjustRadius(self, numberOfSystems):
        if numberOfSystems <= RseData.RADIUS_ADJUSTMENT_INCREASE:
            self.radiusExponent += 1
            if self.radiusExponent > RseData.MAX_RADIUS:
                self.radiusExponent = 10
            if __debug__: print("found {0} systems, increasing radius to {1}".format(numberOfSystems, self.calculateRadius(self.radiusExponent)))
        elif numberOfSystems >= RseData.RADIUS_ADJUSTMENT_DECREASE:
            self.radiusExponent -= 1
            if self.radiusExponent < 0:
                self.radiusExponent = 0
            if __debug__: print("found {0} systems, decreasing radius to {1}".format(numberOfSystems, self.calculateRadius(self.radiusExponent)))

    def calculateRadius(self, value):
        return 39 + 11 * (2 ** value)

    def generateListsFromDatabase(self, x, y, z):
        self.openDatabase()
        if not self.isDatabaseAccessible():
            return False

        sql = " ".join([
            "SELECT id, name, x, y, z, uncertainty, action_todo FROM systems WHERE",
            "systems.x BETWEEN %(x1)s AND %(x2)s AND",
            "systems.y BETWEEN %(y1)s AND %(y2)s AND",
            "systems.z BETWEEN %(z1)s AND %(z2)s AND",
            "deleted_at IS NULL;"
        ])
        systems = list()
        # make sure that the between statements are BETWEEN lower limit AND higher limit
        self.c.execute(sql, {
            "x1": x - self.calculateRadius(self.radiusExponent),
            "x2": x + self.calculateRadius(self.radiusExponent),
            "y1": y - self.calculateRadius(self.radiusExponent),
            "y2": y + self.calculateRadius(self.radiusExponent),
            "z1": z - self.calculateRadius(self.radiusExponent),
            "z2": z + self.calculateRadius(self.radiusExponent)
        })
        for _row in self.c.fetchall():
            _, name, x2, y2, z2, uncertainty, action = _row
            distance = EliteSystem.calculateDistance(x, x2, y, y2, z, z2)
            if distance <= self.calculateRadius(self.radiusExponent):
                eliteSystem = EliteSystem(*_row)
                eliteSystem.distance = distance
                eliteSystem.action_text = ", ".join(
                    [self.projectsDict[project] for project in self.projectsDict.keys() if (eliteSystem.action & project) == project])
                systems.append(eliteSystem)

        # filter out systems that have been completed
        systems = filter(lambda system: system.id not in self.filter, systems)
        systems.sort(key=lambda l: l.distance)

        self.systemList = systems
        self.adjustRadius(len(self.systemList))

        self.closeDatabase()
        return True

    def initializeDictionaries(self):
        self.openDatabase()
        if not self.isDatabaseAccessible():
            return  # database not loaded

        if len(self.projectsDict) == 0:
            self.c.execute("SELECT id,action_text FROM projects")
            self.projectsDict = dict()
            for _row in self.c.fetchall():
                _id, action_text = _row
                self.projectsDict[_id] = action_text
        self.closeDatabase()
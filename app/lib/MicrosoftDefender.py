import time
import pathlib
import requests
import json
import datetime
from datetime import datetime, timedelta
import gzip
import shutil
import os

from app.config.conf import MicrosoftDefenderConfig, MACHINE_ACTION_STATUS, IOC_FIELD_MAPPINGS
from app.lib.Models import Evidence, Indicator
from app.lib.Database import Database


class MicrosoftDefender:
    """
    Wrapper class for Microsoft Defender for Endpoint API calls
    Import this class to retrive alerts, evidences and start live response jobs
    """

    def __init__(self, log):
        """
        Initialize and authenticate the MicrosoftDefender instance, use MicrosoftDefenderConfig as configuration
        :param log: logger instance
        :return: void
        """
        self.access_token = None
        self.headers = None
        self.config = MicrosoftDefenderConfig
        self.log = log
        self.db = Database(log)

        self.authenticate()

    def authenticate(self):
        """
        Authenticate using Azure Active Directory application properties, and retrieves the access token
        :raise: Exception when credentials/application properties are not properly configured
        :return: void
        """

        # defining request body with application properties and secret
        body = {
            "resource": self.config.API.RESOURCE_APPLICATION_ID_URI,
            "client_id": self.config.API.APPLICATION_ID,
            "client_secret": self.config.API.APPLICATION_SECRET,
            "grant_type": "client_credentials"
        }

        # posting defined request data to retrieve access token
        try:
            response = requests.post(url=self.config.API.AUTH_URL, data=body)
            data = json.loads(response.content)
            self.access_token = data["access_token"]
            self.headers = {"Authorization": "Bearer %s" % self.access_token, "User-Agent": self.config.API.USER_AGENT, "Content-Type": "application/json"}
            self.log.debug("Successfully authenticated the Microsoft Defender for Endpoint API")
        except Exception as err:
            self.log.error(err)
            raise

    def get_evidences_from_alerts(self):
        """
        Retrieve alerts and related evidence information
        https://docs.microsoft.com/en-us/microsoft-365/security/defender-endpoint/get-alerts
        :exception: when alerts and evidences are not properly retrieved
        :return evidences: dictionary of evidence objects
        """

        # defining start_time for alerts with using configured TIME_SPAN
        # we need to use UTC because Microsoft Defender for Endpoint stores timestamps as UTC
        start_time = (datetime.utcnow() - timedelta(seconds=self.config.TIME_SPAN)).strftime('%Y-%m-%dT%H:%M:%SZ')

        # defining ODATA Query string for filtering alerts based on start_time, status and severity
        odata_query = "$filter=alertCreationTime+ge+%s" % start_time
        odata_query += " and status in ('%s')" % "','".join(self.config.ALERT.STATUSES)
        odata_query += " and severity in ('%s')" % "','".join(self.config.ALERT.SEVERITIES)

        # adding necessary filter to retrieve evidences with alerts
        odata_query += "&$expand=evidence&"

        # adding necessary filter to define max alert count per request
        odata_query += "$top=%d" % self.config.ALERT.MAX_ALERT_COUNT

        # building final request url with odata query above
        request_url = self.config.API.URL + "/api/alerts?" + odata_query

        # defining initial dictionary which stores evidence objects
        evidences = {}

        # try-except block for handling api request exceptions
        try:
            # making api call with odata query and loading response as json
            response = requests.get(url=request_url, headers=self.headers)
            json_response = json.loads(response.content)

            # if api response contains the "error" key, should be an error about request
            if "error" in json_response:
                self.log.error("Failed to retrieve alerts - Error: %s" % json_response["error"]["message"])
            else:
                # value key in json response contains alerts
                # we checking the "value" key as a second error control
                if "value" in json_response:
                    alerts = json_response["value"]
                    self.log.info("Successfully retrieved %d alerts" % (len(alerts)))

                    # iterating alerts and retrieving evidence data to create Evidence objects
                    for alert in alerts:
                        # try-except block for handling dictionary key related exceptions
                        try:
                            alert_id = alert["id"]
                            alert_severity = alert["severity"]
                            machine_id = alert["machineId"]

                            for evidence in alert["evidence"]:
                                sha256 = evidence["sha256"]

                                # evidence is processed only when entityType is matched with the configured entity types.
                                if evidence["entityType"] in self.config.ALERT.EVIDENCE_ENTITY_TYPES:

                                    # if sha256 is empty or none, continue
                                    if sha256 is not None and sha256.lower() != "none":

                                        if self.db.check_alert_evidence_exists(alert_id + sha256):
                                            self.log.debug("Evidence %s already processed by connector" % sha256)
                                            continue

                                        self.db.insert_alert_evidence(alert_id + sha256)

                                        # if the same evidence file already processed, we need to combine alert_id and machine_id with the old one
                                        # otherwise we need create new Evidence objects and add it to dictionary
                                        if sha256 in evidences.keys():
                                            evidences[sha256].alerts.add(alert_id)
                                            evidences[sha256].machines.add(machine_id)
                                        else:
                                            evidences[sha256] = Evidence(alert_id,
                                                                         alert_severity,
                                                                         sha256,
                                                                         evidence["sha1"],
                                                                         evidence["fileName"],
                                                                         evidence["filePath"],
                                                                         machine_id)
                        except Exception as err:
                            self.log.warning("Failed to parse alert object - Error: %s" % err)
                    self.log.info("Successfully retrieved %d evidences from %d alerts" % (len(evidences), len(alerts)))
                else:
                    self.log.error("Failed to parse api response - Error: value key not found in dict.")
        except Exception as err:
            self.log.error("Failed to retrieve alerts - Error: %s" % err)
        return evidences

    def get_indicators(self):
        """
        Retrieve unique indicators from Microsoft Defender for Endpoint
        https://docs.microsoft.com/en-us/microsoft-365/security/defender-endpoint/get-ti-indicators
        :exception: when indicators are not properly retrieved
        :return indicators: set of indicators
        """

        # building request url with url and necessary path
        request_url = self.config.API.URL + "/api/indicators"

        # defining initial set for storing indicator values
        indicators = set()

        # try-except block for handling api request and parsing exceptions
        try:
            # making api call and loading response as json
            response = requests.get(url=request_url, headers=self.headers)
            json_response = json.loads(response.content)

            # if api response contains the "error" key, should be an error about request
            if "error" in json_response:
                self.log.error("Failed to retrieve indicators - Error: %s" % json_response["error"]["message"])
                return indicators
            else:
                # value key in json response contains indicators
                # we checking the "value" key as a second error control
                if "value" in json_response:
                    for indicator in json_response["value"]:
                        # adding only value to check duplicates easily
                        indicators.add(indicator["indicatorValue"])
                else:
                    self.log.error("Failed to retrieve indicators - Error: value key not found")
                    return indicators
        except Exception as err:
            self.log.error("Failed to retrieve indicators - Error %s" % err)
            return indicators

        self.log.info("%d unique indicator retrieved in total" % (len(indicators)))

        return indicators

    def run_live_response(self, machines):
        """
        Run live response jobs for gathering evidence files from machines
        https://docs.microsoft.com/en-us/microsoft-365/security/defender-endpoint/run-live-response
        :param machines: list of machine objects
        :exception: when live response job is not successful or machine is not available
        :return machines: list of machine objects with downloaded file info
        """

        # iterating machines to start live response jobs
        for machine in machines:
            self.log.info(
                "Waiting %d live response jobs to start for machine %s" % (len(machine.evidences), machine.id))

            # loop until the machine timeout is exceeded or machine has no pending tasks
            # we use MACHINE_TIMEOUT / SLEEP to check machine availability multiple in timeout duration
            while self.config.MACHINE_ACTION.MACHINE_TIMEOUT / self.config.MACHINE_ACTION.SLEEP > machine.timeout_counter and machine.has_pending_actions():

                # checking the machine availability
                # if machine is not available sleep with configured time
                if self.is_machine_available(machine.id):

                    # iterate machine evidences to gather file with live response jobs
                    for evidence in machine.evidences:

                        # second check for machine availability
                        # if machine is not available sleep with configured time again
                        # this control checks newly created (in this for loop) live response jobs status
                        if self.is_machine_available(machine.id):

                            # json request body for live response
                            live_response_command = {
                                "Commands": [
                                    {"type": "GetFile", "params": [{"key": "Path", "value": evidence.absolute_path}]}],
                                "Comment": "VMRay Connector File Acquisition Job for %s" % evidence.sha256
                            }

                            self.log.info("Trying to start live response job for evidence %s from machine %s" % (evidence.absolute_path, machine.id))

                            # building request url with necessary endpoint and machine id
                            request_url = self.config.API.URL + "/api/machines/%s/runliveresponse" % machine.id

                            # try-except block for handling api request exceptions
                            try:
                                # making api call with request body and loading response as json
                                response = requests.post(request_url, data=json.dumps(live_response_command),
                                                         headers=self.headers)
                                json_response = json.loads(response.content)

                                # if api response contains the "error" key, should be an error about request
                                # if there is an error, set live response status failed
                                if "error" in json_response:
                                    self.log.error("Live response error for machine %s for evidence %s - Error: %s" % (
                                        machine.id, evidence.sha256, json_response["error"]["message"]))
                                    evidence.live_response.has_error = True
                                else:
                                    # try-except block for handling parsing exceptions
                                    try:
                                        # second request to load live response info
                                        # this request added for fixing a bug in Microsoft Defender Endpoint Api
                                        # in the first request commands list return empty
                                        # but according to api documentation it should be loaded
                                        # for further info please visit the link below
                                        # https://docs.microsoft.com/en-us/microsoft-365/security/defender-endpoint/run-live-response?view=o365-worldwide#response-example

                                        time.sleep(5)
                                        json_response = self.get_machine_action(json_response["id"])

                                        if json_response is not None:

                                            # iterate api response and create live_response object for evidences
                                            for command in json_response["commands"]:
                                                if command["command"]["type"] == "GetFile":
                                                    evidence.live_response.start(command["index"],
                                                                                 command["errors"],
                                                                                 command["commandStatus"],
                                                                                 datetime.utcnow(),
                                                                                 json_response["id"])
                                            self.log.info(
                                                "Live response job %s for evidence %s started successfully" % (
                                                    evidence.live_response.id, evidence.sha256))

                                            # waiting live response to finish
                                            evidence = self.wait_live_response(evidence)

                                            # if live response job successfully finished
                                            if evidence.live_response.is_finished:

                                                # retrieve live response result and set evidence download_url
                                                evidence_result = self.get_live_response_result(evidence)
                                                if evidence_result is not None:
                                                    evidence.download_url = evidence_result
                                    except Exception as err:
                                        self.log.error("Failed to parse api response for machine %s - Error: %s" % (
                                            machine.id, err))
                                        evidence.live_response.has_error = True
                            except Exception as err:
                                self.log.error(
                                    "Failed to create live response job for machine %s - Error: %s" % (machine.id, err))
                                evidence.live_response.has_error = True
                        else:
                            # waiting the machine for pending live response jobs
                            time.sleep(self.config.MACHINE_ACTION.SLEEP / 60)
                else:
                    # waiting the machine for pending live response jobs
                    time.sleep(self.config.MACHINE_ACTION.SLEEP)

                    # increment timeout_counter to check timeout in While loop
                    machine.timeout_counter += 1

            # if machine has pending actions and timeout hit stop processing the machine and move to next
            if machine.has_pending_actions():
                self.log.error("Machine %s was not available during the timeout (%s seconds)" % (
                    machine.id, self.config.MACHINE_ACTION.MACHINE_TIMEOUT))

        return machines

    def wait_live_response(self, evidence):
        """
        Waiting live response machine action job to finish with configured timeout checks
        :param evidence: evidence object
        :return evidence: modified evidence object with status
        """
        # begin
        self.log.info(
            "Waiting live response job %s for evidence %s to finish" % (evidence.live_response.id, evidence.sha256))

        # loop until the live response job timeout is exceeded or live response job failed/finished
        # we use JOB_TIMEOUT / SLEEP to check job status multiple in timeout duration
        while self.config.MACHINE_ACTION.JOB_TIMEOUT / self.config.MACHINE_ACTION.SLEEP > evidence.live_response.timeout_counter \
                and not evidence.live_response.has_error \
                and not evidence.live_response.is_finished:

            # initial sleep for newly created live response job
            time.sleep(self.config.MACHINE_ACTION.SLEEP)

            # retrieve live response job detail and status
            machine_action = self.get_machine_action(evidence.live_response.id)

            # if there is an error with machine action, set live response status failed
            # else process the machine_action details
            if machine_action is not None:

                # if machine action status is SUCCEEDED, set live response status finished
                if machine_action["status"] == MACHINE_ACTION_STATUS.SUCCEEDED:
                    self.log.info(
                        "Live response job %s for evidence %s finished" % (evidence.live_response.id, evidence.sha256))
                    evidence.live_response.status = machine_action["status"]
                    evidence.live_response.is_finished = True

                # if machine action status is FAIL, set live response status failed
                elif machine_action["status"] in MACHINE_ACTION_STATUS.FAIL:
                    self.log.error("Live response job %s for evidence %s failed with error - Error: %s" % (
                        evidence.live_response.id, evidence.sha256, machine_action["commands"][0]["errors"][0]))
                    evidence.live_response.status = machine_action["status"]
                    evidence.live_response.has_error = True

                # else increment the live response timeout counter to check timeout in While loop
                else:
                    evidence.live_response.timeout_counter += 1
            else:
                evidence.live_response.has_error = True

        # if job timeout limit is exceeded, set live response status failed
        if self.config.MACHINE_ACTION.JOB_TIMEOUT / self.config.MACHINE_ACTION.SLEEP <= evidence.live_response.timeout_counter:
            error_message = "Live response job timeout was hit (%s seconds)" % self.config.MACHINE_ACTION.JOB_TIMEOUT
            self.log.error("Live response job %s for evidence %s failed with error - Error: %s" % (
                evidence.live_response.id, evidence.sha256, error_message))
            evidence.live_response.has_error = True
            evidence.live_response.status = MACHINE_ACTION_STATUS.TIMEOUT

            # cancel machine action to proceed other evidences in machines
            self.cancel_machine_action(evidence.live_response)
            # waiting cancelled machine action to stop
            time.sleep(self.config.MACHINE_ACTION.SLEEP)

        return evidence

    def get_live_response_result(self, evidence):
        """
        Retrieve live response result and download url
        https://docs.microsoft.com/en-us/microsoft-365/security/defender-endpoint/get-live-response-result
        :param evidence: evidence object instance
        :exception: when live response result is not properly retrieved
        :return: dict of live response result or None if there is an error
        """

        # building request url with necessary endpoing and live response id and index
        request_url = self.config.API.URL + "/api/machineactions/%s/GetLiveResponseResultDownloadLink(index=%s)" % (
            evidence.live_response.id, evidence.live_response.index)

        # try-except block for handling api request exceptions
        try:
            # making api call and loading response as json
            response = requests.get(url=request_url, headers=self.headers)
            json_response = json.loads(response.content)

            # if api response contains the "error" key, should be an error about request
            if "error" in json_response:
                self.log.error("Failed to retrieve live response results for %s - Error: %s" % (
                    evidence.live_response.id, json_response["error"]["message"]))
                return None
            else:
                # value key in json response contains indicators
                # we checking the "value" key as a second error control
                if "value" in json_response:
                    return json_response["value"]
                else:
                    self.log.error("Failed to retrieve live response results for %s - Error: value key not found" % (
                        evidence.live_response.id))
                    return None
        except Exception as err:
            self.log.error(
                "Failed to retrieve live response results for %s - Error: %s" % (evidence.live_response.id, err))
            return None

    def download_evidences(self, evidences):
        """
        Download and extract evidence files
        :param evidences: list of evidence objects
        :exception: when evidence file is not properly downloaded or extracted
        :return evidences: list of evicende objects with downloaded file_path
        """

        # initial list to store successfully downloaded evidences
        downloaded_evidences = []
        self.log.info("Downloading %d evidences" % len(evidences))

        for evidence in evidences:
            if evidence.download_url is not None:
                self.log.info("Downloading evidence %s" % evidence.sha256)

                # try-except block for handling download request errors
                try:
                    # download file and store it in response object
                    response = requests.get(evidence.download_url, stream=True)

                    # initialize path variables for downloaded file
                    file_path = self.config.DOWNLOAD.ABSOLUTE_PATH / pathlib.Path(evidence.sha256 + ".gz")
                    unzipped_file_path = self.config.DOWNLOAD.ABSOLUTE_PATH / pathlib.Path(evidence.sha256)
                    self.log.info("Evidence %s downloaded successfully. Response code: %d" % (
                        evidence.sha256, response.status_code))

                    # try-except block for handling file write errors
                    try:
                        # writing downloaded evidence file into disk as chunks
                        with open(file_path, "wb") as file:
                            for chunk in response.iter_content(1024):
                                if chunk:
                                    file.write(chunk)
                        self.log.info("Evidence %s saved successfully" % evidence.sha256)

                        # try-except block for handling gzip extraction errors
                        try:
                            # extracting gzip saved file
                            with gzip.open(file_path, "rb") as compressed:
                                with open(unzipped_file_path, "wb") as decompressed:
                                    shutil.copyfileobj(compressed, decompressed)

                            # if extracting successfull, delete gzip file
                            os.remove(file_path)
                            self.log.info("Evidence %s extracted successfully" % evidence.sha256)

                            # set evidence file path and append it to list
                            evidence.download_file_path = unzipped_file_path
                            downloaded_evidences.append(evidence)
                        except Exception as err:
                            self.log.error("Failed to extract evidence %s - Error: %s" % (evidence.sha256, err))
                    except Exception as err:
                        self.log.error(
                            "Failed to write evidence %s to %s - Error: %s" % (evidence.sha256, file_path, err))
                except Exception as err:
                    self.log.error("Failed to download evidence %s - Error: %s" % (evidence.sha256, err))
        return downloaded_evidences

    def is_machine_available(self, machine_id):
        """
        Check if the machine has no pending or processing machine action
        Because we can't make another machine action request when one of them pending
        :param machine_id: Microsoft Defender for Endpoint ID for the machine
        :return bool: machine availability status
        """

        # retrieving past machine action for machine
        machine_actions = self.get_machine_actions(machine_id)

        # if machine action is None, should be an error
        if machine_actions is not None:

            for action in machine_actions:

                # checking machine action status with configured values
                # if there is at least one pending or in_progress live response jobs, return False
                if action["status"] in MACHINE_ACTION_STATUS.NOT_AVAILABLE:
                    self.log.warning("Machine %s is busy. Current action type is %s and status is %s" % (
                        machine_id, action["type"], action["status"]))
                    return False

            # if there is no pending jobs, return True
            self.log.info("Machine %s is available" % machine_id)
            return True
        else:
            return False

    def get_machine_actions(self, machine_id):
        """
        Retrieve machine actions for given machine_id
        https://docs.microsoft.com/en-us/microsoft-365/security/defender-endpoint/get-machineactions-collection
        :param machine_id: Microsoft Defender for Endpoint ID for the machine
        :exception: when machine actions are not properly retrieved
        :return list or None: list of machine actions or None if there is an error
        """

        # defining ODATA Query string for filtering machine actions based on machine_id
        odata_query = "$filter=machineId+eq+'%s'" % machine_id

        # building request url with odata query
        request_url = self.config.API.URL + "/api/machineactions?" + odata_query

        # try-except block for handling api request and parsing exceptions
        try:
            # making api call with odata query and loading response as json
            response = requests.get(url=request_url, headers=self.headers)
            json_response = json.loads(response.content)

            # if api response contains the "error" key, should be an error about request
            if "error" in json_response:
                self.log.error("Failed to retrieve machine actions for machine %s - Error: %s" % (
                    machine_id, json_response["error"]["message"]))
                return None
            else:
                # value key in json response contains machine actions
                # we checking the "value" key as a second error control
                if "value" in json_response:
                    return json_response["value"]
                else:
                    self.log.error(
                        "Failed to parse api response for machine %s - Error: value key not found in dict" % (
                            machine_id))
                    return None
        except Exception as err:
            self.log.error("Failed to retrieve machine actions for machine %s - Error: %s" % (machine_id, err))
            return None

    def get_machine_action(self, live_response_id):
        """
        Retrieve machine action detail with given live_response_id string
        https://docs.microsoft.com/en-us/microsoft-365/security/defender-endpoint/get-machineaction-object
        :param live_response_id: live response id
        :exception: when machine action is not properly retrieved
        :return dict or None: dict of machine action data or None if there is an error
        """

        # building request url with necessary endpoint and live response machine action id
        request_url = self.config.API.URL + "/api/machineactions/%s" % live_response_id

        # try-except block for handling api request exceptions
        try:
            # making api call and loading response as json
            response = requests.get(url=request_url, headers=self.headers)
            json_response = json.loads(response.content)

            # if api response contains the "error" key, should be an error about request
            if "error" in json_response:
                self.log.error("Failed to retrieve machine action detail for %s - Error: %s" % (
                    live_response_id, json_response["error"]["message"]))
                return None
            else:
                return json_response
        except Exception as err:
            self.log.error("Failed to retrieve machine action for %s - Error: %s" % (live_response_id, err))
            return None

    def cancel_machine_action(self, live_response):
        """
        Cancel the machine action with given live_response object
        https://docs.microsoft.com/en-us/microsoft-365/security/defender-endpoint/cancel-machine-action
        :param live_response: live response instance
        :exception: when machine action is not properly cancelled
        :return bool: status of cancellation request
        """

        # building request url with necessary endpoint and live response machine action id
        request_url = self.config.API.URL + "/api/machineactions/%s/cancel" % live_response.id

        # try-except block for handling api request exceptions
        try:
            # json request body for cancellation request
            request_data = {"Comment": "Machine action was cancelled by VMRay Connector due to timeout"}

            # making api call with request body and loading response as json
            response = requests.post(url=request_url, data=json.dumps(request_data), headers=self.headers)
            json_response = json.loads(response.content)

            # if api response contains the "error" key, should be an error about request
            # if there is an error, return False
            if "error" in json_response:
                self.log.error(
                    "Failed to cancel machine action for %s - Error: %s" % (live_response.id, json_response["error"]))
                return False
            self.log.info("Cancelled live response action %s" % (live_response.id))
            return True
        except Exception as err:
            self.log.error("Failed to cancel machine action for %s - Error: %s" % (live_response.id, err))
            return False

    def create_indicator_objects(self, indicator_data, old_indicators):
        """
        Create indicators objects based on VMRay Analyzer indicator data and retrieved indicators from Microsoft Defender for Endpoint
        :param indicator_data: dict of indicators which retrieved from VMRay submission
        :param old_indicators: set of indicators which retrieved from Microsoft Defender for Endpoint
        :return indicator_objects: list of indicator objects
        """

        indicator_objects = []

        # iterate indicator types
        for key in indicator_data:

            # if configured IOC_FIELD_MAPPINGS dict has indicator type as key
            if key in IOC_FIELD_MAPPINGS.keys():

                # iterate IOC_FIELD_MAPPINGS values to map VMRay indicator types to Microsoft Defender for Endpoint
                for indicator_field in IOC_FIELD_MAPPINGS[key]:
                    indicator_value = indicator_data[key]

                    for indicator in indicator_value:

                        # duplicate check with old indicators
                        if indicator not in old_indicators:
                            indicator_objects.append(Indicator(type=indicator_field,
                                                               value=indicator,
                                                               action=self.config.INDICATOR.ACTION,
                                                               application=self.config.API.APPLICATION_NAME,
                                                               title=self.config.INDICATOR.TITLE,
                                                               description=self.config.INDICATOR.DESCRIPTION))

        return indicator_objects

    def submit_indicators(self, indicators):
        """
        Submit indicators to Microsoft Defender for Endpoint
        https://docs.microsoft.com/en-us/microsoft-365/security/defender-endpoint/post-ti-indicator
        :param indicators: list of indicator objects
        :exception: when indicators are not submitted properly
        :return void:
        """
        self.log.info("%d indicators submitting to Microsoft Defender for Endpoint" % len(indicators))

        # building request url with necessary endpoint
        request_url = self.config.API.URL + "/api/indicators"

        for indicator in indicators:
            # try-except block for handling api request errors
            try:
                # send post request with indicator object as json body
                response = requests.post(url=request_url, data=json.dumps(indicator.serialize()), headers=self.headers)
                if response.status_code == 200:
                    self.log.debug("Indicator %s submitted successfully" % indicator.value)
                else:
                    self.log.error("Failed to submit indicator - Error: %s" % response.content)
            except Exception as err:
                self.log.error("Failed to submit indicator %s - Error: %s" % (indicator.value, err))

    def isolate_machine(self, evidence):
        """
        Isolate machines for given evidence
        https://docs.microsoft.com/en-us/microsoft-365/security/defender-endpoint/isolate-machine
        :param evidence: evidence object
        :exception: when isolation failed
        :return void:
        """

        self.log.info("Isolating %d machines" % len(evidence.machines))

        # iterating machines which contains given evidence
        for machine_id in evidence.machines:

            self.log.info("Isolating machine %s" % machine_id)

            # building request url with necessary endpoint and machine_id
            request_url = self.config.API.URL + "/api/machines/%s/isolate" % machine_id

            # set timeout_count for machine availability checks
            timeout_count = 0

            # set jos status flag for while loop
            is_job_pending = True

            # loop until machine action timeout exceeded or job finished/failed
            while self.config.MACHINE_ACTION.MACHINE_TIMEOUT / self.config.MACHINE_ACTION.SLEEP > timeout_count and is_job_pending:

                # check if machine available
                if self.is_machine_available(machine_id):

                    # try-except block for handling api request exceptions
                    try:

                        # building isolation request body as dict
                        request_data = {
                            "Comment": self.config.MACHINE_ACTION.ISOLATION.COMMENT,
                            "IsolationType": self.config.MACHINE_ACTION.ISOLATION.TYPE
                        }

                        # making api call loading response as json
                        response = requests.post(request_url, data=json.dumps(request_data), headers=self.headers)
                        json_response = json.loads(response.content)

                        # if api response contains the "error" key, should be an error about request
                        if "error" in json_response:
                            self.log.error("Failed to isolate machine %s - Error: %s" % (
                                machine_id, json_response["error"]["message"]))
                            is_job_pending = False
                        else:
                            self.log.info("Machine %s isolation job started" % machine_id)
                            is_job_pending = False

                    except Exception as err:
                        self.log.error("Failed to isolate machine %s - Error: %s" % (machine_id, err))
                        is_job_pending = False
                else:
                    # increment timeout counter for timeout check
                    timeout_count += 1

                    # sleep and wait pending machine actions to finish
                    time.sleep(self.config.MACHINE_ACTION.SLEEP)

            # if timeout exceeded, machine action job has failed
            if self.config.MACHINE_ACTION.MACHINE_TIMEOUT / self.config.MACHINE_ACTION.SLEEP <= timeout_count:
                error_message = "Machine was not available during the timeout (%s seconds)" % self.config.MACHINE_ACTION.MACHINE_TIMEOUT
                self.log.error("Failed to isolate machine %s - Error: %s" % (machine_id, error_message))
            # else wait successful machine action job to finish
            else:
                time.sleep(self.config.MACHINE_ACTION.SLEEP)

    def run_antivirus_scan(self, evidence):
        """
        Run anti virus scan on machines for given evidence
        https://docs.microsoft.com/en-us/microsoft-365/security/defender-endpoint/run-av-scan
        :param evidence: evidence object
        :exception: when isolation failed
        :return void:
        """
        self.log.info("Running antivirus scan on %d machines" % len(evidence.machines))

        # iterating machines which contains given evidence
        for machine_id in evidence.machines:

            self.log.info("Running antivirus scan on machine %s" % machine_id)

            # building request url with necessary endpoint and machine_id
            request_url = self.config.API.URL + "/api/machines/%s/runAntiVirusScan" % machine_id

            # set timeout_count for machine availability checks
            timeout_count = 0

            # set jos status flag for while loop
            is_job_pending = True

            # loop until machine action timeout exceeded or job finished/failed
            while self.config.MACHINE_ACTION.MACHINE_TIMEOUT / self.config.MACHINE_ACTION.SLEEP > timeout_count and is_job_pending:

                # check if machine is available
                if self.is_machine_available(machine_id):

                    # try-except block for handling api request exceptions
                    try:

                        # building isolation request body as dict
                        request_data = {
                            "Comment": self.config.MACHINE_ACTION.ANTI_VIRUS_SCAN.COMMENT,
                            "ScanType": self.config.MACHINE_ACTION.ANTI_VIRUS_SCAN.TYPE
                        }

                        # making api call loading response as json
                        response = requests.post(request_url, data=json.dumps(request_data), headers=self.headers)
                        json_response = json.loads(response.content)

                        # if api response contains the "error" key, should be an error about request
                        if "error" in json_response:
                            self.log.error("Failed to run anti virus scan on machine %s - Error: %s" % (
                                machine_id, json_response["error"]["message"]))
                            is_job_pending = False
                        else:
                            self.log.info("Machine %s anti virus scan job started" % machine_id)
                            is_job_pending = False

                    except Exception as err:
                        self.log.error("Failed to run anti virus scan on machine %s - Error: %s" % (machine_id, err))
                        is_job_pending = False
                else:
                    # increment timeout counter for timeout check
                    timeout_count += 1

                    # sleep and wait pending machine actions to finish
                    time.sleep(self.config.MACHINE_ACTION.SLEEP)

            # if timeout exceeded, machine action job has failed
            if self.config.MACHINE_ACTION.MACHINE_TIMEOUT / self.config.MACHINE_ACTION.SLEEP <= timeout_count:
                error_message = "Machine was not available during the timeout (%s seconds)" % self.config.MACHINE_ACTION.MACHINE_TIMEOUT
                self.log.error("Failed to run anti virus scan on machine %s - Error: %s" % (machine_id, error_message))
            # else wait successful machine action job to finish
            else:
                time.sleep(self.config.MACHINE_ACTION.SLEEP)

    def collect_investigation_package(self, evidence):
        """
        Collect investigation package from machines for given evidence
        https://docs.microsoft.com/en-us/microsoft-365/security/defender-endpoint/collect-investigation-package
        :param evidence: evidence object
        :exception: when isolation failed
        :return void:
        """
        self.log.info("Collecting investigation package on %d machines" % len(evidence.machines))

        # iterating machines which contains given evidence
        for machine_id in evidence.machines:

            self.log.info("Collecting investigation package on machine %s" % machine_id)

            # building request url with necessary endpoint and machine_id
            request_url = self.config.API.URL + "/api/machines/%s/collectInvestigationPackage" % machine_id

            # set timeout_count for machine availability checks
            timeout_count = 0

            # set jos status flag for while loop
            is_job_pending = True

            # loop until machine action timeout exceeded or job finished/failed
            while self.config.MACHINE_ACTION.MACHINE_TIMEOUT / self.config.MACHINE_ACTION.SLEEP > timeout_count and is_job_pending:

                # check if machine is available
                if self.is_machine_available(machine_id):

                    # try-except block for handling api request exceptions
                    try:

                        # building isolation request body as dict
                        request_data = {
                            "Comment": self.config.MACHINE_ACTION.COLLECT_INVESTIGATION_PACKAGE.COMMENT,
                        }

                        # making api call loading response as json
                        response = requests.post(request_url, data=json.dumps(request_data), headers=self.headers)
                        json_response = json.loads(response.content)

                        # if api response contains the "error" key, should be an error about request
                        if "error" in json_response:
                            self.log.error("Failed to collect investigation package on machine %s - Error: %s" % (
                                machine_id, json_response["error"]["message"]))
                            is_job_pending = False
                        else:
                            self.log.info("Machine %s investigation package collection job started" % machine_id)
                            is_job_pending = False

                    except Exception as err:
                        self.log.error(
                            "Failed to collect investigation package on machine %s - Error: %s" % (machine_id, err))
                        is_job_pending = False
                else:
                    # increment timeout counter for timeout check
                    timeout_count += 1

                    # sleep and wait pending machine actions to finish
                    time.sleep(self.config.MACHINE_ACTION.SLEEP)

            # if timeout exceeded, machine action job has failed
            if self.config.MACHINE_ACTION.MACHINE_TIMEOUT / self.config.MACHINE_ACTION.SLEEP <= timeout_count:
                error_message = "Machine was not available during the timeout (%s seconds)" % self.config.MACHINE_ACTION.MACHINE_TIMEOUT
                self.log.error(
                    "Failed to collect investigation package on machine %s - Error: %s" % (machine_id, error_message))
            # else wait successful machine action job to finish
            else:
                time.sleep(self.config.MACHINE_ACTION.SLEEP)

    def stop_and_quarantine_file(self, evidence):
        """
        Stop and quarantine evidence file for affected machines
        https://docs.microsoft.com/en-us/microsoft-365/security/defender-endpoint/stop-and-quarantine-file
        :param evidence: evidence object
        :exception: when isolation failed
        :return void:
        """
        self.log.info("Stop and Quarantine file job starting on %d machines for evidence %s" % (
            len(evidence.machines), evidence.sha256))

        # iterating machines which contains given evidence
        for machine_id in evidence.machines:

            self.log.info(
                "Stop and Quarantine file job starting on machine %s for evidence %s" % (machine_id, evidence.sha256))

            # building request url with necessary endpoint and machine_id
            request_url = self.config.API.URL + "/api/machines/%s/StopAndQuarantineFile" % machine_id

            # set timeout_count for machine availability checks
            timeout_count = 0

            # set jos status flag for while loop
            is_job_pending = True

            # loop until machine action timeout exceeded or job finished/failed
            while self.config.MACHINE_ACTION.MACHINE_TIMEOUT / self.config.MACHINE_ACTION.SLEEP > timeout_count and is_job_pending:

                # check if machine is available
                if self.is_machine_available(machine_id):

                    # try-except block for handling api request exceptions
                    try:

                        # building isolation request body as dict
                        request_data = {
                            "Comment": self.config.MACHINE_ACTION.ANTI_VIRUS_SCAN.COMMENT,
                            "Sha1": evidence.sha1
                        }

                        # making api call loading response as json
                        response = requests.post(request_url, data=json.dumps(request_data), headers=self.headers)
                        json_response = json.loads(response.content)

                        # if api response contains the "error" key, should be an error about request
                        if "error" in json_response:
                            self.log.error("Failed to stop and quarantine evidence %s on machine %s - Error: %s" % (
                                evidence.sha256, machine_id, json_response["error"]["message"]))
                            is_job_pending = False
                        else:
                            self.log.info("Stop and quarantine job started for evidence %s on machine %s" % (
                                evidence.sha256, machine_id))
                            is_job_pending = False

                    except Exception as err:
                        self.log.error("Failed to stop and quarantine evidence %s on machine %s - Error: %s" % (
                            evidence.sha256, machine_id, err))
                        is_job_pending = False
                else:
                    # increment timeout counter for timeout check
                    timeout_count += 1

                    # sleep and wait pending machine actions to finish
                    time.sleep(self.config.MACHINE_ACTION.SLEEP)

            # if timeout exceeded, machine action job has failed
            if self.config.MACHINE_ACTION.MACHINE_TIMEOUT / self.config.MACHINE_ACTION.SLEEP <= timeout_count:
                error_message = "Machine was not available during the timeout (%s seconds)" % self.config.MACHINE_ACTION.MACHINE_TIMEOUT
                self.log.error("Failed to stop and quarantine evidence %s on machine %s - Error: %s" % (
                    evidence.sha256, machine_id, error_message))
            # else wait successful machine action job to finish
            else:
                time.sleep(self.config.MACHINE_ACTION.SLEEP)

    def run_automated_machine_actions(self, sample_data, evidence):
        """
        Running automated machine actions based on confiuration and VMRay Analyzer results
        :param sample_data: VMRay response for submitted sample
        :param evidence: evidence object
        :return void:
        """

        # Collecting investigation package from affected machines if configuration is active and verdict is selected
        if self.config.MACHINE_ACTION.COLLECT_INVESTIGATION_PACKAGE.ACTIVE and \
                sample_data["sample_verdict"] in self.config.MACHINE_ACTION.COLLECT_INVESTIGATION_PACKAGE.VERDICTS:
            self.collect_investigation_package(evidence)

        # Running anti virus scan on affected machines if configuration is active and verdict is selected
        if self.config.MACHINE_ACTION.ANTI_VIRUS_SCAN.ACTIVE and \
                sample_data["sample_verdict"] in self.config.MACHINE_ACTION.ANTI_VIRUS_SCAN.VERDICTS:
            self.run_antivirus_scan(evidence)

        # Stop and quarantine file in affected machines if configuration is active and verdict is selected
        if self.config.MACHINE_ACTION.STOP_AND_QUARANTINE_FILE.ACTIVE and \
                sample_data["sample_verdict"] in self.config.MACHINE_ACTION.STOP_AND_QUARANTINE_FILE.VERDICTS:
            self.stop_and_quarantine_file(evidence)

        # Isolate affected machines if configuration is active and verdict is selected
        if self.config.MACHINE_ACTION.ISOLATION.ACTIVE and \
                sample_data["sample_verdict"] in self.config.MACHINE_ACTION.ISOLATION.VERDICTS:
            self.isolate_machine(evidence)

    def enrich_alerts(self, evidence, sample_data, sample_vtis):
        """
        Enrich alerts with VMRay Analyzer submission metadata
        https://docs.microsoft.com/en-us/microsoft-365/security/defender-endpoint/update-alert
        :param evidence: evidence object
        :param sample_data: dict object which contains summary data about the sample
        :param sample_vtis: dict object which contains parsed VTI data about the sample
        :exception: when alert is not updated properly
        :return void:
        """

        # building comment object as text

        # adding evidence sha256
        comment = "Evidence SHA256:\n"
        comment += sample_data["sample_sha256hash"] + "\n\n"

        # adding VMRay Analyzer Verdict
        comment += "VMRAY Analyzer Verdict: %s\n\n" % sample_data["sample_verdict"].upper()

        # adding VMRay Analyzer sample url
        comment += "Sample Url:\n"
        comment += sample_data["sample_webif_url"] + "\n\n"

        # adding VMRay Analyzer sample classifications
        comment += "Classifications:\n"
        comment += "\n".join(sample_data["sample_classifications"]) + "\n\n"

        # adding VMRay Analyzer threat names
        comment += "Threat Names:\n"
        comment += "\n".join(sample_data["sample_threat_names"]) + "\n\n"

        # adding VMRay Analyzer VTI's
        comment += "VTI's:\n"
        comment += "\n".join(list(set([vti["operation"] for vti in sample_vtis]))) + "\n\n"

        # itareting alerts related with given evidences
        for alert_id in evidence.alerts:

            # try-except block for handling api request exceptions
            try:

                # building request body as json
                request_data = {"comment": comment}

                # building request_url with necessary endpoint and given alert_id
                request_url = self.config.API.URL + "/api/alerts/%s" % alert_id

                # making api call
                response = requests.patch(request_url, data=json.dumps(request_data), headers=self.headers)

                if response.status_code != 200:
                    self.log.error("Failed to update alert %s - Error: %s" % (alert_id, response.content))

            except Exception as err:
                self.log.error("Failed to update alert %s - Error: %s" % (alert_id, err))

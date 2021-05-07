import os
import traceback
import yaml
import json
import validators
import requests
import logging
import tldextract

from celery import shared_task
from discord_webhook import DiscordWebhook
from reNgine.celery import app
from startScan.models import ScanHistory, Subdomain, ScanActivity, EndPoint, Vulnerability
from targetApp.models import Domain
from notification.models import NotificationHooks
from scanEngine.models import EngineType
from django.conf import settings
from django.utils import timezone, dateformat
from django.shortcuts import get_object_or_404

from celery import shared_task
from datetime import datetime

from django.conf import settings
from django.utils import timezone, dateformat
from django.shortcuts import get_object_or_404
from django.core.exceptions import ObjectDoesNotExist

from reNgine.celery import app
from reNgine.definitions import *

from startScan.models import ScanHistory, Subdomain, ScanActivity, EndPoint
from targetApp.models import Domain
from notification.models import NotificationHooks
from scanEngine.models import EngineType, Configuration, Wordlist

from .common_func import *

'''
task for background scan
'''


@app.task
def doScan(domain_id, scan_history_id, scan_type, engine_type):
    # get current time
    current_scan_time = timezone.now()
    '''
    scan_type = 0 -> immediate scan, need not create scan object
    scan_type = 1 -> scheduled scan
    '''
    if scan_type == 1:
        engine_object = EngineType.objects.get(pk=engine_type)
        domain = Domain.objects.get(pk=domain_id)
        task = ScanHistory()
        task.domain_name = domain
        task.scan_status = -1
        task.scan_type = engine_object
        task.celery_id = doScan.request.id
        task.scan_start_date = current_scan_time
        task.save()
    elif scan_type == 0:
        domain = Domain.objects.get(pk=domain_id)
        task = ScanHistory.objects.get(pk=scan_history_id)

    # save the last scan date for domain model
    domain.last_scan_date = current_scan_time
    domain.save()

    # once the celery task starts, change the task status to Started
    task.scan_status = 1
    task.scan_start_date = current_scan_time
    # task.whois = get_whois(domain.domain_name)
    task.save()

    activity_id = create_scan_activity(task, "Scanning Started", 2)
    results_dir = settings.TOOL_LOCATION + 'scan_results/'
    os.chdir(results_dir)

    try:
        current_scan_dir = domain.domain_name + '_' + \
            str(datetime.strftime(timezone.now(), '%Y_%m_%d_%H_%M_%S'))
        os.mkdir(current_scan_dir)
    except Exception as exception:
        logger.error(exception)
        # do something here
        scan_failed(task)

    yaml_configuration = None

    try:
        yaml_configuration = yaml.load(
            task.scan_type.yaml_configuration,
            Loader=yaml.FullLoader)
        excluded_subdomains = ''
    except Exception as exception:
        logger.error(exception)
        # TODO: Put failed reason on db

    results_dir = results_dir + current_scan_dir

    if yaml_configuration:
        if(task.scan_type.subdomain_discovery):
            activity_id = create_scan_activity(task, "Subdomain Scanning", 1)
            subdomain_scan(
                task,
                domain,
                yaml_configuration,
                results_dir,
                activity_id)
        else:
            skip_subdomain_scan(task, domain, subdomain_scan_results_file)

        update_last_activity(activity_id, 2)
        activity_id = create_scan_activity(task, "HTTP Crawler", 1)
        alive_file_location = results_dir + '/alive.txt'
        http_crawler(
            task,
            domain,
            results_dir,
            alive_file_location,
            activity_id)

        if VISUAL_IDENTIFICATION in yaml_configuration:
            update_last_activity(activity_id, 2)
            activity_id = create_scan_activity(
                task, "Visual Recon - Screenshot", 1)
            grab_screenshot(task, yaml_configuration, results_dir, activity_id)

        if(task.scan_type.port_scan):
            update_last_activity(activity_id, 2)
            activity_id = create_scan_activity(task, "Port Scanning", 1)
            port_scanning(task, yaml_configuration, results_dir, activity_id)

        if(task.scan_type.dir_file_search):
            update_last_activity(activity_id, 2)
            activity_id = create_scan_activity(task, "Directory Search", 1)
            directory_brute(task, yaml_configuration, results_dir, activity_id)

        if(task.scan_type.fetch_url):
            update_last_activity(activity_id, 2)
            activity_id = create_scan_activity(task, "Fetching endpoints", 1)
            fetch_endpoints(
                task,
                domain,
                yaml_configuration,
                results_dir,
                activity_id)

        if(task.scan_type.vulnerability_scan):
            update_last_activity(activity_id, 2)
            activity_id = create_scan_activity(task, "Vulnerability Scan", 1)
            vulnerability_scan(
                task,
                domain,
                yaml_configuration,
                results_dir,
                activity_id)
            update_last_activity(activity_id, 2)

    activity_id = create_scan_activity(task, "Scan Completed", 2)
    update_last_activity(activity_id, 2)

    '''
    Once the scan is completed, save the status to successful
    '''
    if ScanActivity.objects.filter(scan_of=task).filter(status=0).all():
        task.scan_status = 0
    else:
        task.scan_status = 2
    task.stop_scan_date = timezone.now()
    task.save()
    send_notification("reEngine finished scanning " + domain.domain_name)
    # cleanup results
    delete_scan_data(results_dir)
    return {"status": True}


def subdomain_scan(task, domain, yaml_configuration, results_dir, activity_id):
    '''
    This function is responsible for performing subdomain enumeration
    '''
    subdomain_scan_results_file = results_dir + '/sorted_subdomain_collection.txt'
    # Excluded subdomains
    excluded_subdomains = ''
    if EXCLUDED_SUBDOMAINS in yaml_configuration:
        excluded_subdomains = yaml_configuration[EXCLUDED_SUBDOMAINS]

    # check for all the tools and add them into string
    # if tool selected is all then make string, no need for loop
    if 'all' in yaml_configuration[SUBDOMAIN_DISCOVERY][USES_TOOLS]:
        tools = 'amass-active amass-passive assetfinder sublist3r subfinder oneforall'
    else:
        tools = ' '.join(
            str(tool) for tool in yaml_configuration[SUBDOMAIN_DISCOVERY][USES_TOOLS])

    logging.info(tools)

    # check for thread, by default 10
    threads = 10
    if THREAD in yaml_configuration[SUBDOMAIN_DISCOVERY]:
        _threads = yaml_configuration[SUBDOMAIN_DISCOVERY][THREAD]
        if _threads > 0:
            threads = _threads

    if 'amass' in tools:
        amass_config_path = None
        if AMASS_CONFIG in yaml_configuration[SUBDOMAIN_DISCOVERY]:
            short_name = yaml_configuration[SUBDOMAIN_DISCOVERY][AMASS_CONFIG]
            try:
                config = get_object_or_404(
                    Configuration, short_name=short_name)
                '''
                if config exists in db then write the config to
                scan location, and append in amass_command
                '''
                with open(results_dir + '/config.ini', 'w') as config_file:
                    config_file.write(config.content)
                amass_config_path = results_dir + '/config.ini'
            except Exception as e:
                logging.error(CONFIG_FILE_NOT_FOUND)
                pass

        if 'amass-passive' in tools:
            amass_command = AMASS_COMMAND + \
                ' -passive -d {} -o {}/from_amass.txt'.format(
                    domain.domain_name, results_dir)
            if amass_config_path:
                amass_command = amass_command + \
                    ' -config {}'.format(settings.TOOL_LOCATION +
                                         'scan_results/' + amass_config_path)

            # Run Amass Passive
            logging.info(amass_command)
            os.system(amass_command)

        if 'amass-active' in tools:
            amass_command = AMASS_COMMAND + \
                ' -active -d {} -o {}/from_amass_active.txt'.format(
                    domain.domain_name, results_dir)

            if AMASS_WORDLIST in yaml_configuration[SUBDOMAIN_DISCOVERY]:
                wordlist = yaml_configuration[SUBDOMAIN_DISCOVERY][AMASS_WORDLIST]
                if wordlist == 'default':
                    wordlist_path = settings.TOOL_LOCATION + AMASS_DEFAULT_WORDLIST_PATH
                else:
                    wordlist_path = settings.TOOL_LOCATION + 'wordlist/' + wordlist + '.txt'
                    if not os.path.exists(wordlist_path):
                        wordlist_path = settings.TOOL_LOCATION + AMASS_WORDLIST
                amass_command = amass_command + \
                    ' -brute -w {}'.format(wordlist_path)
            if amass_config_path:
                amass_command = amass_command + \
                    ' -config {}'.format(settings.TOOL_LOCATION +
                                         'scan_results/' + amass_config_path)

            # Run Amass Active
            logging.info(amass_command)
            os.system(amass_command)

    if 'assetfinder' in tools:
        assetfinder_command = 'assetfinder --subs-only {} > {}/from_assetfinder.txt'.format(
            domain.domain_name, results_dir)

        # Run Assetfinder
        logging.info(assetfinder_command)
        os.system(assetfinder_command)

    if 'sublist3r' in tools:
        sublist3r_command = 'python3 /app/tools/Sublist3r/sublist3r.py -d {} -t {} -o {}/from_sublister.txt'.format(
            domain.domain_name, threads, results_dir)

        # Run sublist3r
        logging.info(sublist3r_command)
        os.system(sublist3r_command)

    if 'subfinder' in tools:
        subfinder_command = 'subfinder -d {} -t {} -o {}/from_subfinder.txt'.format(
            domain.domain_name, threads, results_dir)

        # Check for Subfinder config files
        if SUBFINDER_CONFIG in yaml_configuration[SUBDOMAIN_DISCOVERY]:
            short_name = yaml_configuration[SUBDOMAIN_DISCOVERY][SUBFINDER_CONFIG]
            try:
                config = get_object_or_404(
                    Configuration, short_name=short_name)
                '''
                if config exists in db then write the config to
                scan location, and append in amass_command
                '''
                with open(results_dir + '/subfinder-config.yaml', 'w') as config_file:
                    config_file.write(config.content)
                subfinder_config_path = results_dir + '/subfinder-config.yaml'
            except Exception as e:
                pass
            subfinder_command = subfinder_command + \
                ' -config {}'.format(subfinder_config_path)

        # Run Subfinder
        logging.info(subfinder_command)
        os.system(subfinder_command)

    if 'oneforall' in tools:
        oneforall_command = 'python3 /app/tools/OneForAll/oneforall.py --target {} run'.format(
            domain.domain_name, results_dir)

        # Run OneForAll
        logging.info(oneforall_command)
        os.system(oneforall_command)

        extract_subdomain = "cut -d',' -f6 /app/tools/OneForAll/results/{}.csv >> {}/from_oneforall.txt".format(
            domain.domain_name, results_dir)

        os.system(extract_subdomain)

        # remove the results from oneforall directory

        os.system(
            'rm -rf /app/tools/OneForAll/results/{}.*'.format(domain.domain_name))

    '''
    All tools have gathered the list of subdomains with filename
    initials as from_*
    We will gather all the results in one single file, sort them and
    remove the older results from_*
    '''

    os.system(
        'cat {}/*.txt > {}/subdomain_collection.txt'.format(results_dir, results_dir))

    '''
    Remove all the from_* files
    '''

    os.system('rm -rf {}/from*'.format(results_dir))

    '''
    Sort all Subdomains
    '''

    os.system(
        'sort -u {}/subdomain_collection.txt -o {}/sorted_subdomain_collection.txt'.format(
            results_dir,
            results_dir))

    '''
    The final results will be stored in sorted_subdomain_collection.
    '''

    # parse the subdomain list file and store in db
    with open(subdomain_scan_results_file) as subdomain_list:
        for _subdomain in subdomain_list:
            if(_subdomain.rstrip('\n') in excluded_subdomains):
                continue
            if validators.domain(_subdomain.rstrip('\n')):
                subdomain = Subdomain()
                subdomain.scan_history = task
                subdomain.target_domain = domain
                subdomain.name = _subdomain.rstrip('\n')
                subdomain.save()


def skip_subdomain_scan(task, domain, subdomain_scan_results_file):
    '''
    When subdomain scanning is not performed, the target itself is subdomain
    '''
    only_subdomain_file.write(domain.domain_name + "\n")
    only_subdomain_file.close()

    scanned = Subdomain()
    scanned.subdomain = domain.domain_name
    scanned.scan_history = task
    scanned.target_domain = domain
    scanned.save()


def http_crawler(task, domain, results_dir, alive_file_location, activity_id):
    '''
    This function is runs right after subdomain gathering, and gathers important
    like page title, http status, etc
    HTTP Crawler runs by default
    '''
    httpx_results_file = results_dir + '/httpx.json'

    subdomain_scan_results_file = results_dir + '/sorted_subdomain_collection.txt'

    httpx_command = 'cat {} | httpx -follow-host-redirects -random-agent -cdn -json -o {} -threads 100'.format(
        subdomain_scan_results_file, httpx_results_file)

    os.system(httpx_command)

    # alive subdomains from httpx
    alive_file = open(alive_file_location, 'w')

    # writing httpx results
    if os.path.isfile(httpx_results_file):
        httpx_json_result = open(httpx_results_file, 'r')
        lines = httpx_json_result.readlines()
        for line in lines:
            json_st = json.loads(line.strip())
            try:
                subdomain = Subdomain.objects.get(
                    scan_history=task, name=json_st['url'].split("//")[-1])
                if 'url' in json_st:
                    subdomain.http_url = json_st['url']
                if 'status-code' in json_st:
                    subdomain.http_status = json_st['status-code']
                if 'title' in json_st:
                    subdomain.page_title = json_st['title']
                if 'content-length' in json_st:
                    subdomain.content_length = json_st['content-length']
                if 'ip' in json_st:
                    subdomain.ip_address = json_st['ip']
                if 'cdn' in json_st:
                    subdomain.is_ip_cdn = json_st['cdn']
                if 'cnames' in json_st:
                    cname_list = ','.join(json_st['cnames'])
                    subdomain.cname = cname_list
                subdomain.discovered_date = timezone.now()
                subdomain.save()
                alive_file.write(json_st['url'] + '\n')
                '''
                Saving Default http urls to EndPoint
                '''
                endpoint = EndPoint()
                endpoint.scan_history = task
                endpoint.target_domain = domain
                endpoint.subdomain = subdomain
                if 'url' in json_st:
                    endpoint.http_url = json_st['url']
                if 'status-code' in json_st:
                    endpoint.http_status = json_st['status-code']
                if 'title' in json_st:
                    endpoint.page_title = json_st['title']
                if 'content-length' in json_st:
                    endpoint.content_length = json_st['content-length']
                if 'content-type' in json_st:
                    endpoint.conteny_type = json_st['content-type']
                endpoint.discovered_date = timezone.now()
                endpoint.save()
            except Exception as exception:
                logging.error(exception)
                update_last_activity(activity_id, 0)

    alive_file.close()

    # sort and unique alive urls
    os.system('sort -u {} -o {}'.format(alive_file_location, alive_file_location))


def grab_screenshot(task, yaml_configuration, results_dir, activity_id):
    '''
    This function is responsible for taking screenshots
    '''
    # after subdomain discovery run aquatone for visual identification
    output_aquatone_path = results_dir + '/aquascreenshots'

    alive_subdomains_path = results_dir + '/alive.txt'

    if PORT in yaml_configuration[VISUAL_IDENTIFICATION]:
        scan_port = yaml_configuration[VISUAL_IDENTIFICATION][PORT]
        # check if scan port is valid otherwise proceed with default xlarge
        # port
        if scan_port not in ['small', 'medium', 'large', 'xlarge']:
            scan_port = 'xlarge'
    else:
        scan_port = 'xlarge'

    if THREAD in yaml_configuration[VISUAL_IDENTIFICATION] and yaml_configuration[VISUAL_IDENTIFICATION][THREAD] > 0:
        threads = yaml_configuration[VISUAL_IDENTIFICATION][THREAD]
    else:
        threads = 10

    if HTTP_TIMEOUT in yaml_configuration[VISUAL_IDENTIFICATION]:
        http_timeout = yaml_configuration[VISUAL_IDENTIFICATION][HTTP_TIMEOUT]
    else:
        http_timeout = 3000  # Default Timeout for HTTP

    if SCREENSHOT_TIMEOUT in yaml_configuration[VISUAL_IDENTIFICATION]:
        screenshot_timeout = yaml_configuration[VISUAL_IDENTIFICATION][SCREENSHOT_TIMEOUT]
    else:
        screenshot_timeout = 30000  # Default Timeout for Screenshot

    if SCAN_TIMEOUT in yaml_configuration[VISUAL_IDENTIFICATION]:
        scan_timeout = yaml_configuration[VISUAL_IDENTIFICATION][SCAN_TIMEOUT]
    else:
        scan_timeout = 100  # Default Timeout for Scan

    aquatone_command = 'cat {} | /app/tools/aquatone --threads {} -ports {} -out {} -http-timeout {} -scan-timeout {} -screenshot-timeout {}'.format(
        alive_subdomains_path, threads, scan_port, output_aquatone_path, http_timeout, scan_timeout, screenshot_timeout)

    logging.info(aquatone_command)
    os.system(aquatone_command)
    os.system('chmod -R 607 /app/tools/scan_results/*')
    aqua_json_path = output_aquatone_path + '/aquatone_session.json'

    try:
        if os.path.isfile(aqua_json_path):
            with open(aqua_json_path, 'r') as json_file:
                data = json.load(json_file)

            for host in data['pages']:
                sub_domain = Subdomain.objects.get(
                    scan_history__id=task.id,
                    subdomain=data['pages'][host]['hostname'])
                # list_ip = data['pages'][host]['addrs']
                # ip_string = ','.join(list_ip)
                # sub_domain.ip_address = ip_string
                sub_domain.screenshot_path = results_dir + \
                    '/aquascreenshots/' + data['pages'][host]['screenshotPath']
                sub_domain.http_header_path = results_dir + \
                    '/aquascreenshots/' + data['pages'][host]['headersPath']
                tech_list = []
                if data['pages'][host]['tags'] is not None:
                    for tag in data['pages'][host]['tags']:
                        tech_list.append(tag['text'])
                tech_string = ','.join(tech_list)
                sub_domain.technology_stack = tech_string
                sub_domain.save()
    except Exception as exception:
        logging.error(exception)
        update_last_activity(activity_id, 0)


def port_scanning(task, yaml_configuration, results_dir, activity_id):
    '''
    This function is responsible for running the port scan
    '''
    subdomain_scan_results_file = results_dir + '/sorted_subdomain_collection.txt'
    port_results_file = results_dir + '/ports.json'

    # check the yaml_configuration and choose the ports to be scanned

    scan_ports = '-'  # default port scan everything
    if PORTS in yaml_configuration[PORT_SCAN]:
        # TODO:  legacy code, remove top-100 in future versions
        all_ports = yaml_configuration[PORT_SCAN][PORTS]
        if 'full' in all_ports:
            naabu_command = 'cat {} | naabu -json -o {} -p {}'.format(
                subdomain_scan_results_file, port_results_file, '-')
        elif 'top-100' in all_ports:
            naabu_command = 'cat {} | naabu -json -o {} -top-ports 100'.format(
                subdomain_scan_results_file, port_results_file)
        elif 'top-1000' in all_ports:
            naabu_command = 'cat {} | naabu -json -o {} -top-ports 1000'.format(
                subdomain_scan_results_file, port_results_file)
        else:
            scan_ports = ','.join(
                str(port) for port in all_ports)
            naabu_command = 'cat {} | naabu -json -o {} -p {}'.format(
                subdomain_scan_results_file, port_results_file, scan_ports)

    # check for exclude ports
    if EXCLUDE_PORTS in yaml_configuration[PORT_SCAN] and yaml_configuration[PORT_SCAN][EXCLUDE_PORTS]:
        exclude_ports = ','.join(
            str(port) for port in yaml_configuration['port_scan']['exclude_ports'])
        naabu_command = naabu_command + \
            ' -exclude-ports {}'.format(exclude_ports)

    if NAABU_RATE in yaml_configuration[PORT_SCAN] and yaml_configuration[PORT_SCAN][NAABU_RATE] > 0:
        naabu_command = naabu_command + \
            ' -rate {}'.format(
                yaml_configuration[PORT_SCAN][NAABU_RATE])
    else:
        naabu_command = naabu_command + ' -t 10'

    # run naabu
    os.system(naabu_command)

    # writing port results
    try:
        port_json_result = open(port_results_file, 'r')
        lines = port_json_result.readlines()
        for line in lines:
            try:
                json_st = json.loads(line.strip())
            except Exception as exception:
                json_st = "{'host':'','port':''}"
            sub_domain = Subdomain.objects.get(
                scan_history=task, subdomain=json_st['host'])
            if sub_domain.open_ports:
                sub_domain.open_ports = sub_domain.open_ports + \
                    ',' + str(json_st['port'])
            else:
                sub_domain.open_ports = str(json_st['port'])
            sub_domain.save()
    except BaseException as exception:
        logging.error(exception)
        update_last_activity(activity_id, 0)


def unusual_port_detection():
    '''
    OSINT
    If port scan is enabled this function will run nmap to identify the unusual
    ports
    '''
    pass


def check_waf():
    '''
    This function will check for the WAF being used in subdomains using wafw00f
    '''
    pass


def directory_brute(task, yaml_configuration, results_dir, activity_id):
    '''
    This function is responsible for performing directory scan
    '''
    # scan directories for all the alive subdomain with http status >
    # 200
    alive_subdomains = Subdomain.objects.filter(
        scan_history__id=task.id).exclude(
        http_url='')
    dirs_results = results_dir + '/dirs.json'

    # check the yaml settings
    if EXTENSIONS in yaml_configuration[DIR_FILE_SEARCH]:
        extensions = ','.join(
            str(port) for port in yaml_configuration[DIR_FILE_SEARCH][EXTENSIONS])
    else:
        extensions = 'php,git,yaml,conf,db,mysql,bak,txt'

    # Threads
    if THREAD in yaml_configuration[DIR_FILE_SEARCH] and yaml_configuration[DIR_FILE_SEARCH][THREAD] > 0:
        threads = yaml_configuration[DIR_FILE_SEARCH][THREAD]
    else:
        threads = 10

    for subdomain in alive_subdomains:
        # /app/tools/dirsearch/db/dicc.txt
        if (WORDLIST not in yaml_configuration[DIR_FILE_SEARCH] or
            not yaml_configuration[DIR_FILE_SEARCH][WORDLIST] or
                'default' in yaml_configuration[DIR_FILE_SEARCH][WORDLIST]):
            wordlist_location = settings.TOOL_LOCATION + 'dirsearch/db/dicc.txt'
        else:
            wordlist_location = settings.TOOL_LOCATION + 'wordlist/' + \
                yaml_configuration[DIR_FILE_SEARCH][WORDLIST] + '.txt'

        dirsearch_command = settings.TOOL_LOCATION + 'get_dirs.sh {} {} {}'.format(
            subdomain.http_url, wordlist_location, dirs_results)
        dirsearch_command = dirsearch_command + \
            ' {} {}'.format(extensions, threads)

        # check if recursive strategy is set to on
        if RECURSIVE in yaml_configuration[DIR_FILE_SEARCH] and yaml_configuration[DIR_FILE_SEARCH][RECURSIVE]:
            dirsearch_command = dirsearch_command + \
                ' {}'.format(
                    yaml_configuration[DIR_FILE_SEARCH][RECURSIVE_LEVEL])

        os.system(dirsearch_command)

        try:
            if os.path.isfile(dirs_results):
                with open(dirs_results, "r") as json_file:
                    json_string = json_file.read()
                    scanned_host = Subdomain.objects.get(
                        scan_history__id=task.id, http_url=subdomain.http_url)
                    scanned_host.directory_json = json_string
                    scanned_host.save()
        except Exception as exception:
            logging.error(exception)
            update_last_activity(activity_id, 0)


def fetch_endpoints(
        task,
        domain,
        yaml_configuration,
        results_dir,
        activity_id):
    '''
    This function is responsible for fetching all the urls associated with target
    and run HTTP probe
    It first runs gau to gather all urls from wayback, then we will use hakrawler to identify more urls
    '''
    # check yaml settings
    if 'all' in yaml_configuration[FETCH_URL][USES_TOOLS]:
        tools = 'gau hakrawler'
    else:
        tools = ' '.join(
            str(tool) for tool in yaml_configuration[FETCH_URL][USES_TOOLS])

    subdomain_scan_results_file = results_dir + '/sorted_subdomain_collection.txt'

    if 'aggressive' in yaml_configuration['fetch_url']['intensity']:
        with open(subdomain_scan_results_file) as subdomain_list:
            for subdomain in subdomain_list:
                if validators.domain(subdomain.rstrip('\n')):
                    print('Fetching URL for ' + subdomain.rstrip('\n'))
                    os.system(
                        settings.TOOL_LOCATION + 'get_urls.sh %s %s %s' %
                        (subdomain.rstrip('\n'), results_dir, tools))

                    url_results_file = results_dir + '/final_httpx_urls.json'

                    urls_json_result = open(url_results_file, 'r')
                    lines = urls_json_result.readlines()
                    for line in lines:
                        json_st = json.loads(line.strip())
                        if not EndPoint.objects.filter(
                                scan_history=task).filter(
                                http_url=json_st['url']).count():
                            endpoint = EndPoint()
                            endpoint.scan_history = task
                            if 'url' in json_st:
                                endpoint.http_url = json_st['url']
                            if 'content-length' in json_st:
                                endpoint.content_length = json_st['content-length']
                            if 'status-code' in json_st:
                                endpoint.http_status = json_st['status-code']
                            if 'title' in json_st:
                                endpoint.page_title = json_st['title']
                            endpoint.discovered_date = timezone.now()
                            endpoint.target_domain = domain
                            if 'content-type' in json_st:
                                endpoint.content_type = json_st['content-type']
                            endpoint.save()
    else:
        os.system(
            settings.TOOL_LOCATION +
            'get_urls.sh {} {} {}'.format(
                domain.domain_name,
                results_dir,
                tools))

        url_results_file = results_dir + '/final_httpx_urls.json'

        try:
            urls_json_result = open(url_results_file, 'r')
            lines = urls_json_result.readlines()
            for line in lines:
                json_st = json.loads(line.strip())
                endpoint = EndPoint()
                endpoint.scan_history = task
                if 'url' in json_st:
                    endpoint.http_url = json_st['url']
                if 'content-length' in json_st:
                    endpoint.content_length = json_st['content-length']
                if 'status-code' in json_st:
                    endpoint.http_status = json_st['status-code']
                if 'title' in json_st:
                    endpoint.page_title = json_st['title']
                if 'content-type' in json_st:
                    endpoint.content_type = json_st['content-type']
                endpoint.discovered_date = timezone.now()
                endpoint.save()
        except Exception as exception:
            logging.error(exception)
            update_last_activity(activity_id, 0)


def vulnerability_scan(
        task,
        domain,
        yaml_configuration,
        results_dir,
        activity_id):
    '''
    This function will run nuclei as a vulnerability scanner
    '''

    vulnerability_result_path = results_dir + '/vulnerability.json'

    nuclei_scan_urls = results_dir + '/unfurl_urls.txt'

    if(task.scan_type.fetch_url):
        os.system('cat {} >> {}'.format(results_dir + '/unfurl_urls.txt', results_dir + '/alive.txt'))
        os.system('sort -u {} -o {}'.format(results_dir + '/alive.txt', results_dir + '/alive.txt'))

    nuclei_scan_urls = results_dir + '/alive.txt'

    nuclei_command = 'nuclei -json -l {} -o {}'.format(
        nuclei_scan_urls, vulnerability_result_path)

    # check yaml settings for templates
    if 'all' in yaml_configuration['vulnerability_scan']['template']:
        template = '/root/nuclei-templates'
    else:
        if isinstance(
                yaml_configuration['vulnerability_scan']['template'],
                list):
            _template = ','.join(
                [str(element) for element in yaml_configuration['vulnerability_scan']['template']])
            template = _template.replace(',', ' -t ')
        else:
            template = yaml_configuration['vulnerability_scan']['template'].replace(
                ',', ' -t ')

    # Update nuclei command with templates
    nuclei_command = nuclei_command + ' -t ' + template

    # # check yaml settings for  concurrency
    # if yaml_configuration['vulnerability_scan']['concurrent'] > 0:
    #     concurrent = yaml_configuration['vulnerability_scan']['concurrent']
    # else:
    #     concurrent = 10
    #
    # # Update nuclei command with concurrent
    # nuclei_command = nuclei_command + ' -c ' + str(concurrent)

    # yaml settings for severity
    if 'severity' in yaml_configuration['vulnerability_scan']:
        if 'all' not in yaml_configuration['vulnerability_scan']['severity']:
            if isinstance(
                    yaml_configuration['vulnerability_scan']['severity'],
                    list):
                _severity = ','.join(
                    [str(element) for element in yaml_configuration['vulnerability_scan']['severity']])
                severity = _severity.replace(" ", "")
            else:
                severity = yaml_configuration['vulnerability_scan']['severity'].replace(
                    " ", "")
        else:
            severity = "critical, high, medium, low, info"

    # update nuclei templates before running scan
    os.system('nuclei -update-templates')
    for _severity in severity.split(","):
        # delete any existing vulnerability.json file
        if os.path.isfile(vulnerability_result_path):
            os.system('rm {}'.format(vulnerability_result_path))
        # run nuclei
        final_nuclei_command = nuclei_command + ' -severity ' + _severity
        logger.info(final_nuclei_command)

        os.system(final_nuclei_command)
        try:
            if os.path.isfile(vulnerability_result_path):
                urls_json_result = open(vulnerability_result_path, 'r')
                lines = urls_json_result.readlines()
                for line in lines:
                    json_st = json.loads(line.strip())
                    host = json_st['host']
                    extracted_subdomain = tldextract.extract(host)
                    _subdomain = '.'.join(extracted_subdomain[:4])
                    if _subdomain[0] == '.':
                        _subdomain = _subdomain[1:]
                    try:
                        subdomain = Subdomain.objects.get(
                            name=_subdomain, scan_history=task)
                        vulnerability = Vulnerability()
                        vulnerability.subdomain = subdomain
                        vulnerability.scan_history = task
                        vulnerability.target_domain = domain
                        try:
                            endpoint = EndPoint.objects.get(
                                scan_history=task, target_domain=domain, http_url=host)
                            vulnerability.endpoint = endpoint
                        except Exception as exception:
                            pass
                        if 'name' in json_st['info']:
                            vulnerability.name = json_st['info']['name']
                        if 'severity' in json_st['info']:
                            if json_st['info']['severity'] == 'info':
                                severity = 0
                            elif json_st['info']['severity'] == 'low':
                                severity = 1
                            elif json_st['info']['severity'] == 'medium':
                                severity = 2
                            elif json_st['info']['severity'] == 'high':
                                severity = 3
                            elif json_st['info']['severity'] == 'critical':
                                severity = 4
                            else:
                                severity = 0
                        else:
                            severity = 0
                        vulnerability.severity = severity
                        if 'tags' in json_st['info']:
                            vulnerability.tags = json_st['info']['tags']
                        if 'description' in json_st['info']:
                            vulnerability.description = json_st['info']['description']
                        if 'reference' in json_st['info']:
                            vulnerability.reference = json_st['info']['reference']
                        if 'matched' in json_st:
                            vulnerability.http_url = json_st['matched']
                        if 'templateID' in json_st:
                            vulnerability.template_used = json_st['templateID']
                        if 'description' in json_st:
                            vulnerability.description = json_st['description']
                        if 'matcher_name' in json_st:
                            vulnerability.matcher_name = json_st['matcher_name']
                        if 'extracted_results' in json_st:
                            vulnerability.extracted_results = json_st['extracted_results']
                        vulnerability.discovered_date = timezone.now()
                        vulnerability.open_status = True
                        vulnerability.save()
                        send_notification(
                            "ALERT! {} vulnerability with {} severity identified in {} \n Vulnerable URL: {}".format(
                                json_st['info']['name'],
                                json_st['info']['severity'],
                                domain.domain_name,
                                json_st['matched']))
                    except ObjectDoesNotExist:
                        logger.error('Object not found')

        except Exception as exception:
            logging.error(exception)
            update_last_activity(activity_id, 0)


def send_notification(message):
    notif_hook = NotificationHooks.objects.filter(send_notif=True)
    scan_status_msg = {}
    headers = {'content-type': 'application/json'}
    for notif in notif_hook:
        if 'slack.com' in notif.hook_url:
            scan_status_msg['text'] = message
            requests.post(
                notif.hook_url,
                data=json.dumps(scan_status_msg),
                headers=headers)
        elif 'discordapp.com' in notif.hook_url:
            webhook = DiscordWebhook(url=notif.hook_url, content=message)
            webhook.execute()


def scan_failed(task):
    task.scan_status = 0
    task.stop_scan_date = timezone.now()
    task.save()


def create_scan_activity(task, message, status):
    scan_activity = ScanActivity()
    scan_activity.scan_of = task
    scan_activity.title = message
    scan_activity.time = timezone.now()
    scan_activity.status = status
    scan_activity.save()
    return scan_activity.id


def update_last_activity(id, activity_status):
    ScanActivity.objects.filter(
        id=id).update(
        status=activity_status,
        time=timezone.now())

def delete_scan_data(results_dir):
    # remove all text files
    os.system('rm -r {}/*.txt'.format(results_dir))
    # remove all json files
    os.system('rm -r {}/*.json'.format(results_dir))
    # remove all html files
    os.system('rm -r {}/*.html'.format(results_dir))


@app.task(bind=True)
def test_task(self):
    print('*' * 40)
    print('test task run')
    print('*' * 40)

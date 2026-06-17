import configparser
import json
import logging
import os
from dataclasses import dataclass
import requests
from requests.auth import HTTPBasicAuth
from datetime import datetime, timedelta
from enum import Enum


class FieldType(Enum):
    STRING: 1
    NUMBER: 2
    RADIO: 3
    RADIOGROUP: 4
    DATETIME: 5
    CHECKBOX: 7


class DataType(Enum):
    STATIC = 'STATIC'
    MONTHLY = 'MONTHLY_CACHE'
    DAILY = 'DAILY_CACHE'


class DataName(Enum):
    ERROR = '上传多维表失败'
    PROJECT = '项目信息'
    MEMBER = '组织成员信息'
    TASK = 'Trinity任务信息'
    BUG = 'Bug数据'
    DEVIATION = '工时偏差数据'
    WORK = '工时数据'


# 飞书机器人
os.environ["APP_ID"] = 'cli_a514aea9fa79900b'
os.environ["APP_SECRET"] = 'IsUeIxmzO5NtJiQA6B3MdfkHqIcmQqws'

# Date YYYY-MM-DD
# DateTime YYYY-MM-DD HH:mm:ss

# Trinity 机器人
ENV = 'prd'
DEV_ENV_URL = "http://hzhio133a370v.v01.net:8180"
DEV_PI_USER = "ALE_HZ_APTDMS_DEV"
DEV_PI_PW = "APTDMS@2099D9"
DEV_TRINITY_USER = "pi_test"
DEV_TRINITY_PW = "spZKLlQGV34HrsDNFATlyg%3D%3D"
DEV_PLATFORM = "test"

# PRD_ENV_URL = "http://POP.v01.net:8180"
PRD_ENV_URL = "https://cop-office.desaysv.com"
PRD_PI_USER = "DQM_PTV_PTV2-CRT29S7J"
PRD_PI_PW = "6qdleWWuf6PaaDbylyunsWbqbyjWldIf"
PRD_TRINITY_USER = "pi_sz_buger"
PRD_TRINITY_PW = "spZKLlQGV34HrsDNFATlyg%3D%3D"
PRD_PLATFORM = "SZ_BUGER"

logging.basicConfig(format='%(asctime)s:%(filename)s:%(thread)d:%(levelname)s:%(process)d:%(message)s',
                    # filename='./run.log',
                    filemode='a',
                    level=logging.INFO)

current_dir = os.getcwd()
cache_dir = os.path.join(current_dir, 'cache')
RECORD_CACHE_DIR = cache_dir

mysql_config = {
    'user': 'gyso',
    'password': '2wsx#EDC',
    'host': '10.219.221.165',
    'database': 'szpmo',
    'raise_on_warnings': True,
}

# task_config = {
#     'url': "https://yesv-desaysv.feishu.cn/base/Af0SbQRYhaP8GCsVKcocQrzsnyd?table=tblVdY9eJdpTDSfj&view=vewYJVqI5S",
#     'fields_filter': ['taskId', 'title', 'status', 'timeSpent', 'assigneeId', 'progress', 'initialEstimate',
#                       'remainingEstimate', 'authorId', 'description', 'planStartDate', 'planEndDate',
#                       'actualStartDate', 'actualEndDate', 'reviewerId', 'created', 'updated', 'projectId',
#                       'projectActualEndDate', 'projectName', 'projectType', 'projectStatus'],
#     'insert_info_by': 'assigneeId',
# }

# task_config = {
#     'url': "https://yesv-desaysv.feishu.cn/base/QkGBbw9CzafJPTsJs1fc0fWpnHc?table=tblKUxFKnGn5Rijm&view=vewMVgaOjF",  # 把这里换成你的表格链接
#     'fields_filter': ['taskId', 'initialEstimate', 'timeSpent'],  # 只保留这3个
#     'insert_info_by': 'assigneeId',
# }
task_config = {
    'url': "https://yesv-desaysv.feishu.cn/base/QkGBbw9CzafJPTsJs1fc0fWpnHc?table=tblKUxFKnGn5Rijm&view=vewMVgaOjF",
    'fields_filter': ['taskId', 'title', 'status', 'timeSpent', 'assigneeId', 'progress', 'initialEstimate',
                      'remainingEstimate', 'authorId', 'description', 'planStartDate', 'planEndDate',
                      'actualStartDate', 'actualEndDate', 'reviewerId', 'created', 'updated', 'projectId',
                      'projectActualEndDate', 'projectName', 'projectType', 'projectStatus'],
    'insert_info_by': 'assigneeId',
}

bug_config = {
    'url': "https://yesv-desaysv.feishu.cn/base/Af0SbQRYhaP8GCsVKcocQrzsnyd?table=tblRpUDXqxtgrqUS&view=vewSSJjA00",
    'fields_filter': ['bugId', 'title', 'projectName', 'status', 'severity', 'age', 'assignee', 'originDeptId',
                      'created', 'updated', 'timeSpent', 'planStartDate', 'projectId'],
    'insert_info_by': 'assignee',
}

work_deviation_config = {
    'url': "https://yesv-desaysv.feishu.cn/base/Af0SbQRYhaP8GCsVKcocQrzsnyd?table=tblw30qNTy1sCg1F&view=vewXCCnRow",
    'fields_filter': ['accountName', 'date', 'workHour', 'overTime', 'attendanceHour',
                      'fillInWorkHour', 'deviationRate', 'account'],
    'insert_info_by': 'account',
}

work_hours_config = {
    'url': "https://yesv-desaysv.feishu.cn/base/Af0SbQRYhaP8GCsVKcocQrzsnyd?table=tblT6hjk9jPhjNc7&view=vew6Er2Blt",
    'fields_filter': ['workitemId', 'uid', 'timeSpent', 'projectName', 'projectType', 'sz_manager', 'workType',
                      'workDate', 'comment', 'projectId', 'projectStatus', 'customer', 'customerName',
                      'projectCategory',
                      'title', 'status'],
    'insert_info_by': 'uid',
}

member_config = {
    'url': "https://yesv-desaysv.feishu.cn/base/Af0SbQRYhaP8GCsVKcocQrzsnyd?table=tblbdC90LQ3bGSpD&view=vewmbCUwyX",
    'fields_filter': ['uid', 'cname', 'department', 'group_name', 'group_full_name'],
}

project_config = {
    'url': "https://yesv-desaysv.feishu.cn/base/Af0SbQRYhaP8GCsVKcocQrzsnyd?table=tblRV06hF6lyfc1w&view=vewYTgNkSd",
    'fields_filter': ['projectId', 'projectName', 'sz_manager', 'sz_participate', 'bu', 'status', 'typeName',
                      'authorId',
                      'actualStartDate', 'actualEndDate', 'endDate', 'customer', 'modelName', 'actualSOPDate', 'type'],
}

robot_sync_info_config = {
    'url': "https://yesv-desaysv.feishu.cn/base/Af0SbQRYhaP8GCsVKcocQrzsnyd?table=tblviY26DHoCW4Pc&view=vewsSghHFn",
    'fields_filter': ['id', 'syncDate', 'dataName', 'dataType', 'is_success', 'msg'],
}

target_uids = ['uid03519', 'uids1287', 'uidq8536', 'uidq8710', 'uidq5308', 'uids0105', 'uid02619', 'uid02687',
               'uid03071']
target_projectIds = ['APP2025112011470332510', #GAC T75 VCC 智驾版_ADC
                     'PMD2024032711450436649', #DF NISSAN_LK1A_IPU04_Orin N
                     'APP2026031715280267816', #GTMC AY5-TM  ADCU
                     'PMD2024032913445922116', #GAC_AY5_IPU04_单OrinX
                     'PMD2024072216452174162', #GAC_AY3_IPU04_ADC_单OrinX
                     'PMD2024060620130134965', #GAC_T68_IPU04_ADC_OrinX
                     'PMD2025031920265349050', #GAC_T9M_IPU04_ADC_OrinX
                     'APP2026011710212336366', #郑州日产S20 角毫米波雷达（2944方案）
                     'APP2026011710350226176', #郑州日产S20前毫米波雷达（4D边缘计算）
                     'PMD2025060612224000976', #X1T ThorU/GAC_XE6_IPU14_ThorU
                     'PMD2024042617351857420', #GAC_AH8_IPU04_单OrinX
                     'PMD2024060720191019789', #GTMC_AY5-T_IPU04_ADC_OrinX
                     'PMD2025031311181640944', #GAC_A8R_IPU04_单OrinX
                     'PMD2024120221095107834', #GTMC_A66-T_IPU04_ADC_OrinY
                     'PMD2022112210380404993', #GAC A02 IPU04 单Orin
                     'PMD2023020722181365027', #广汽 A19 单Orin IPU04
                     'PMD2024022210122841195', #GAC_A02Y_IPU04_ADC_OrinX
                     'PMD2024022311142017745', #GAC_A19_IPU04_ADC_OrinX
                     'PMD2024120210121168624', #DF NISSAN PK1B_IPU04_Orin Y
                     'OPP2026031109364479995', #BYD AI BOX潜在项目预研究
                     'APP2025122309411137097', #BTET BZ5 ADCU
                     'PMD2025062410141202455', # Honda 3GA/3GE 8650 Project
                     'PMD2024013019344108426', #BEV_Step3_ADC
                     'OPP2025120309151836674', #BTET BZ5 预研究
                     'APP2026022710074832602', #ZNA_S20_ADAS 郑州日产S20智驾
                     'OPP2025091616590192840', #郑州日产8775 S20 舱驾一体域控预研项目
                     'APP2026042119304398954'  #Honda 3DAA
                     ]

org_codes = ['50021024', '50021028', '50021026', '50022826', '50021025', '50021027', '50024024']

# 字符串 1，数字 2，日期 5，bool 7
field_type = {
    'account': 1,
    'type': 1,
    'age': 2,
    'timeSpent': 2,
    'progress': 2,
    'workHour': 2,
    'overTime': 2,
    'attendanceHour': 2,
    'fillInWorkHour': 2,
    'planEndDate': 5,
    'actualStartDate': 5,
    'actualEndDate': 5,
    'projectActualEndDate': 5,
    'planStartDate': 5,
    'date': 5,
    'syncDate': 5,
    'endDate': 5,
    'modelName': 5,
    'actualSOPDate': 5,
    'workDate': 5,
    'is_success': 7,
    'sz_manager': 7,
    'sz_participate': 7,
}

field_zh_name_dict = {
    'taskId': '任务Id',
    'assigneeId': '成员ID',
    'progress': '进度',
    'initialEstimate': '预估时间',
    'remainingEstimate': '剩余时间',
    'authorId': '创建者',
    'description': '描述',
    'planEndDate': '计划结束时间',
    'actualStartDate': '实际开始时间',
    'actualEndDate': '实际结束时间',
    'reviewerId': 'ReviewerId',
    'projectActualEndDate': '项目实际技术时间',
    'bugId': 'BugId',
    'severity': '故障等级',
    'age': '故障Age',
    'assignee': '当前处理者',
    'originDeptId': '所在组织',
    'created': '创建时间',
    'updated': '更新时间',
    'planStartDate': '计划开始处理时间',
    'accountName': 'AccountName',
    'date': '日期',
    'workHour': '工时',
    'overTime': '加班工时',
    'attendanceHour': '考勤工时',
    'fillInWorkHour': '填写工时',
    'deviationRate': '工时偏差',
    'account': 'Account',
    'workitemId': 'TaskId',
    'timeSpent': '耗时',
    'projectName': '项目名',
    'projectType': '项目类型',
    'sz_manager': '是否为纳管项目',
    'workType': '工作类别',
    'workDate': '工作日期',
    'comment': '工作内容',
    'projectId': '项目ID',
    'projectStatus': '项目状态',
    'customer': '客户',
    'customerName': '客户名称',
    'projectCategory': '项目类别',
    'title': 'Title',
    'status': '状态',
    'uid': '成员ID',
    'cname': '名称',
    'department': '部门',
    'group_name': '学科ID',
    'group_full_name': '学科名称',
    'sz_participate': '是否深圳参与项目',
    'bu': 'BU',
    'typeName': '类别名',
    'endDate': '结束日期',
    'modelName': '模块名称',
    'actualSOPDate': 'SOP日期',
    'type': '类型',
    'id': 'ID',
    'syncDate': '同步时间',
    'dataName': '数据项',
    'dataType': '频率类别',
    'is_success': '是否同步成功',
    'msg': '信息'
}

if not os.path.exists(RECORD_CACHE_DIR):
    os.makedirs(RECORD_CACHE_DIR)


def save_to_cache(data, file_name):
    target_filepath = os.path.join(RECORD_CACHE_DIR, file_name)
    with open(target_filepath, 'w', encoding='utf-8') as file:
        file.write(data)
    return target_filepath


@dataclass
class RequestInfo:
    host = DEV_ENV_URL
    pi_user = DEV_PI_USER
    pi_pw = DEV_PI_PW
    trinity_user = DEV_TRINITY_USER
    trinity_pw = DEV_TRINITY_PW
    platform = DEV_PLATFORM


def get_request_info():
    r = RequestInfo()
    if ENV != 'dev':
        r.host = PRD_ENV_URL
        r.pi_user = PRD_PI_USER
        r.pi_pw = PRD_PI_PW
        r.trinity_user = PRD_TRINITY_USER
        r.trinity_pw = PRD_TRINITY_PW
        r.platform = PRD_PLATFORM
    return r


def request_token():
    request_info = get_request_info()
    url = rf'{request_info.host}/hzsv/Trinity/Trinity_GetToken'

    data = {
        "uid": request_info.trinity_user,
        "pwd": request_info.trinity_pw
    }

    response = requests.post(
        url,
        json=data,
        auth=HTTPBasicAuth(request_info.pi_user, request_info.pi_pw)
    )

    if response.status_code == 200:
        response_data = response.json()
        if response_data.get("code") == 200:
            token = response_data['data']['token']
            logging.info(f"refresh Token response_data: {response_data}")
            token_cache = response_data['data']
            time = datetime.now().strftime("%Y%m%d%H%M")
            save_to_cache(json.dumps(token_cache), f"gettoken_{time}")
            return token, True
    else:
        logging.info(f"Failed to get token. Status code: {response.status_code}")
        logging.info(f"Response: {response.text}")
    return "null", False


def get_token():
    configure = configparser.ConfigParser()
    try:
        if os.path.exists('./trinity.ini'):
            configure.read('trinity.ini', encoding='utf-8')
            expire_time = configure.get("TOKEN", "expire_time")
            token = configure.get("TOKEN", "token")
            expiration_datetime = datetime.strptime(expire_time, "%Y%m%d%H%M")
            now = datetime.now()
            logging.info(f"expire_time={expire_time}, expiration_datetime={expiration_datetime},"
                         f" isExpired = {now > expiration_datetime}")
            if now < expiration_datetime:
                return token
    except Exception as e:
        logging.exception(f"Read ini file error {e}")
    finally:
        if not os.path.exists('./trinity.ini'):
            with open('./trinity.ini', 'w', encoding='utf-8') as cf:
                configure["TOKEN"] = {"expire_time": "NA", 'token': "NA"}
                configure.write(cf)

    with open('./trinity.ini', 'w', encoding='utf-8') as cf:
        token, is_OK = request_token()
        if is_OK:
            expiration_timestamp = datetime.now() + timedelta(hours=12)
            expiration_time = expiration_timestamp.strftime("%Y%m%d%H%M")
            configure["TOKEN"] = {"expire_time": expiration_time, 'token': token}
            configure.write(cf)
        else:
            logging.error("Error request token!!")
            raise Exception("Request token exception!!")

    token = configure.get('TOKEN', 'token')
    logging.debug(f'trinity.ini token:{token}')
    return token


trinity_cache_config = {}


def get_trinity_cache_config():
    configure = configparser.ConfigParser()
    configure.read('trinity.ini', encoding='utf-8')
    sz_all_projects_cache = configure.get("STATIC", "sz_all_projects_cache")
    sz_manager_projects_cache = configure.get("STATIC", "sz_manager_projects_cache")

    desay_all_projects_cache = configure.get("MONTHLY_CACHE", "desay_all_projects_cache")
    desay_all_members_cache = configure.get("MONTHLY_CACHE", "desay_all_members_cache")
    sz_all_task_cache = configure.get("MONTHLY_CACHE", "sz_all_task_cache")

    daily_work_deviations_cache = configure.get("DAILY_CACHE", "daily_work_deviations_cache")
    daily_work_hours_cache = configure.get("DAILY_CACHE", "daily_work_hours_cache")
    daily_bugs_cache = configure.get("DAILY_CACHE", "daily_bugs_cache")
    trinity_cache_config.update({
        'sz_all_projects_cache': sz_all_projects_cache,
        'sz_manager_projects_cache': sz_manager_projects_cache,
        'desay_all_projects_cache': desay_all_projects_cache,
        'desay_all_members_cache': desay_all_members_cache,
        'sz_all_task_cache': sz_all_task_cache,
        'daily_work_deviations_cache': daily_work_deviations_cache,
        'daily_work_hours_cache': daily_work_hours_cache,
        'daily_bugs_cache': daily_bugs_cache,
    })
    print(trinity_cache_config)
    return trinity_cache_config


def update_trinity_cache_config(cache_info: dict, data_type: DataType):
    key = list(cache_info.keys())[0]
    value = list(cache_info.values())[0]
    configure = configparser.ConfigParser()
    configure.read('trinity.ini')
    configure[str(data_type.value)][key] = value
    with open('./trinity.ini', 'w', encoding='utf-8') as cf:
        configure.write(cf)
    trinity_cache_config.update(cache_info)
    pass


if __name__ == '__main__':
    # t = get_token()
    get_trinity_cache_config()
    update_trinity_cache_config({'sz_all_task_cache': 'cache/cache_task_202407132030'}, DataType.MONTHLY)
    # print(f"configure test get token={t}")

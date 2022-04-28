import os
import boto3
import json, logging
import traceback
import urllib.request
from datetime import datetime, timedelta, timezone

logger = logging.getLogger()
logger.setLevel(logging.INFO)

JST = timezone(timedelta(hours=+9), 'JST')

# 
verbose_notification = False
aws_region = os.getenv("AWS_REGION", 'ap-northeast-1')
slack_bot_token = os.getenv("SLACK_BOT_TOKEN", 'xoxb-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx')
slack_channel = os.getenv('SLACK_CHANNEL', 'xxxxxxxx')
schedule_tag = os.getenv('SCHEDULE_TAG', 'ec2-snoozable-shutdown')

reminder_template = """
[
		{
			"type": "section",
			"text": {
				"type": "plain_text",
				"text": "%message-text%"
			}
		},
		{
			"type": "divider"
		},
		{
			"type": "actions",
			"elements": [
				{
					"type": "button",
					"text": {
						"type": "plain_text",
						"text": "1時間延長する",
						"emoji": true
					},
					"style": "primary",
					"value": "%machine%;%datetime-1%"
				},
				{
					"type": "button",
					"text": {
						"type": "plain_text",
						"text": "3時間延長する",
						"emoji": true
					},
					"style": "primary",
					"value": "%machine%;%datetime-2%"
				},
				{
					"type": "button",
					"text": {
						"type": "plain_text",
						"text": "今日はシャットダウンしない",
						"emoji": true
					},
					"style": "danger",
					"value": "%machine%;%datetime-3%"
				}
			]
		}
]
"""

def parse_tag(instance, key):
    t = [t for t in instance.tags if t['Key'] == key]
    if len(t) > 0:
        return t[0]['Value']
    return None

def instance_desc(instance):
    # return f'{instance.id}({parse_tag(instance, "Name")})'
    return f'{parse_tag(instance, "Name")}'

def delete_remind(ts):
    url = "https://slack.com/api/chat.delete"
    headers = {
        "Content-Type": "application/json; charset=UTF-8",
        "Authorization": f"Bearer {slack_bot_token}"
    }

    data = {
        "channel": slack_channel,
        "ts": ts,
    }
    
    req = urllib.request.Request(url, json.dumps(data).encode(), headers)
    with urllib.request.urlopen(req) as res:
        body = res.read()
        jbody = json.loads(body)
        return jbody['ts']

def post_message(message):
    url = "https://slack.com/api/chat.postMessage"
    headers = {
        "Content-Type": "application/json; charset=UTF-8",
        "Authorization": f"Bearer {slack_bot_token}"
    }

    data = {
        "channel": slack_channel,
        "blocks": json.dumps(message),
    }
    logger.info(message)
    
    req = urllib.request.Request(url, json.dumps(data).encode(), headers)
    with urllib.request.urlopen(req) as res:
        body = res.read()
        jbody = json.loads(body)
        return jbody['ts']

def post_plain(message):
    template = """[
		{
			"type": "section",
			"text": {
				"type": "plain_text",
				"emoji": true,
				"text": "%message%"
			}
		}
    ]
"""
    m = template
    m = m.replace("%message%", message)
    message = json.loads(m)

    return post_message(message)

def post_remind(instance, stopTime):
    def stoptime(dt, ts):
        d = dt + ts
        return d.strftime('%Y-%m-%d_%H:%M:%S%z')

    m = reminder_template
    stopStr = stopTime.strftime('%H時%M分')
    m = m.replace("%message-text%", f'{instance_desc(instance)} は {stopStr} に停止予定ですよ')
    m = m.replace('%machine%', instance.id)
    m = m.replace('%datetime-1%', stoptime(stopTime, timedelta(hours=1)))
    m = m.replace('%datetime-2%', stoptime(stopTime, timedelta(hours=3)))
    m = m.replace('%datetime-3%', stoptime(stopTime, timedelta(days=1)))
    message = json.loads(m)

    return post_message(message)

def autoSnoozeByCpu(instance, data):
    try:
        if 'autoSnoozeCpuThreshold' not in data:
            return False
        thresold = int(data['autoSnoozeCpuThreshold'])

        if thresold > 0:
            cloudwatch = boto3.client("cloudwatch", region_name=aws_region)
            response = cloudwatch.get_metric_statistics(
                    Namespace='AWS/EC2',
                    MetricName='CPUUtilization',
                    Dimensions=[
                    {
                        'Name': 'InstanceId',
                        'Value': instance.id
                    },
                    ],
                    StartTime=datetime.utcnow() - timedelta(seconds=600),
                    EndTime=datetime.utcnow(),
                    Period=300,
                    Statistics=['Average']
            )
            average = response['Datapoints'][0]['Average'] * 100

            if average > thresold:
                # 自動延長
                stopTime = datetime.now(JST) + timedelta(hours=3)
                logger.info(f'update shutdownSchedule {stopTime}')
                data['shutdownSchedule'] = stopTime.strftime('%Y-%m-%d %H:%M:%S%z')
                post_plain(f'{instance_desc(instance)} の停止時刻を自動延長しました。次回チェック:{stopTime.strftime('%Y-%m-%d %H:%M')} , CPU 使用率/停止閾値={average:.2f}/{thresold}%)')
                return True

        return False
    except BaseException as ex:
        logger.info(str(ex))
    return False

def process_running(instance, data):
    if 'shutdownSchedule' not in data:
        now = datetime.now(JST)
        h = int(data['defaultShutdown'][:2])
        m = int(data['defaultShutdown'][2:])
        d = datetime(now.year, now.month, now.day, h, m, 0, tzinfo=JST)
        if d < now:
            # 既に停止予定時刻を過ぎていれば明日の同時刻
            now = now + timedelta(days=1)
            d = datetime(now.year, now.month, now.day, h, m, 0, tzinfo=JST)
        logger.info(f'set shutdownSchedule {d}')
        data['shutdownSchedule'] = d.strftime('%Y-%m-%d %H:%M:%S%z')
    else:
        now = datetime.now(JST)
        stopTime = datetime.strptime(data['shutdownSchedule'], '%Y-%m-%d %H:%M:%S%z')
        remindTime = stopTime - timedelta(minutes=int(data['remind']))
        
        if now > stopTime:
            # シャットダウン実行
            logger.info('invoke shutdown...')
            instance.stop()

            if 'sendRemind' in data:
                delete_remind(data['sendRemind'])

        elif now > remindTime and 'sendRemind' not in data:
            # 自動延長(CPU利用率が設定値以上の場合、自動で伸ばす)
            if autoSnoozeByCpu(instance, data):
                pass
            else:
                # リマインド送信
                logger.info('send shutdown remind')
                data['sendRemind'] = post_remind(instance, stopTime)
        
    if 'state' in data and data['state'] != "running":
        logger.info('state -> running')
        if verbose_notification:
            post_plain(f'{instance_desc(instance)} が起動しました。自動停止予定時刻は {data["shutdownSchedule"]} です。')
        

    data['state'] = 'running'
    return data

    
def process_stopped(instance, data):
    if "state" in data and data['state'] != "stopped":
        logger.info('state -> stopped')
        if verbose_notification:
            post_plain(f'{instance_desc(instance)} が停止しました。')
        
    if 'shutdownSchedule' in data:
        del data['shutdownSchedule']
    if 'sendRemind' in data:
        del data['sendRemind']

    data['state'] = 'stopped'
    return data
    
def ec2_poll():
    ec2 = boto3.resource('ec2', aws_region)
    instances = ec2.instances.filter(
        Filters=[{'Name': 'instance-state-name', 'Values': ['pending','running','stopping','stopped']}])

    for instance in instances:
        try:
            logger.info(f"EC2 instance {instance.id}({parse_tag(instance, 'Name')})")
            sdata = parse_tag(instance, schedule_tag)
            if sdata is None:
                continue
            
            data = json.loads(sdata)

            newData = None        
            stateName = instance.state['Name']
            if stateName == 'running':
                newData = process_running(instance, data)
            if stateName == 'stopped':
                newData = process_stopped(instance, data)
            
            if newData:
                tags = [{
                    "Key" : schedule_tag,
                    "Value" : json.dumps(newData)
                }]
                instance.create_tags(Tags=tags)

        except BaseException as e:
            logger.error(f'{str(e)}')
            print(traceback.format_exc())
            
    logger.info('ec2_poll completed.')

def handle_action(action_value, response_url):
    # 停止予定時刻の更新
    ec2 = boto3.resource('ec2', aws_region)
    instances = ec2.instances.filter(
        Filters=[{'Name': 'instance-state-name', 'Values': ['pending','running','stopping','stopped']}])

    values = action_value.split(';')
    instance = [i for i in instances if i.id == values[0]][0]

    stopTime = datetime.strptime(values[1], '%Y-%m-%d_%H:%M:%S%z')

    sdata = parse_tag(instance, schedule_tag)
    data = json.loads(sdata)

    logger.info(f'update shutdownSchedule {stopTime}')

    data['shutdownSchedule'] = stopTime.strftime('%Y-%m-%d %H:%M:%S%z')
    del data['sendRemind']

    tags = [{
        "Key" : schedule_tag,
        "Value" : json.dumps(data)
    }]
    instance.create_tags(Tags=tags)

    # 元メッセージ削除
    url = "https://slack.com/api/chat.postMessage"
    headers = {
        "Content-Type": "application/json; charset=UTF-8"
    }

    data = {
        "delete_original": True,
    }
    
    req = urllib.request.Request(response_url, json.dumps(data).encode(), headers)
    with urllib.request.urlopen(req) as res:
        body = res.read()
        logger.info(body)

    #
    if verbose_notification:
        post_plain(f'{instance_desc(instance)} の自動停止時刻を {stopTime} に延長しました。')


def lambda_handler(event, context):
    logger.info(event)

    try:
        body = urllib.parse.unquote(event['body'])
        body = body.lstrip('payload=\n')
        body = json.loads(body)
        action_value = body['actions'][0]['value']
        logger.info(f'accept action {action_value}')
        handle_action(action_value, body['response_url'])

    except BaseException as e:
        ec2_poll()
        
    return {
        'statusCode': 200,
        'body': json.dumps('Hello from Lambda!')
    }

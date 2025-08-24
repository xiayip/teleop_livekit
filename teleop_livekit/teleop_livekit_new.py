#!/usr/bin/env python3
"""
统一机器人控制器 - 支持Topic/Service/Action三种ROS通信范式
使用LiveKit进行公网传输
"""

import asyncio
import json
import time
import threading
from enum import Enum
from typing import Dict, Any, Optional, Callable
from dataclasses import dataclass, asdict
import uuid

# LiveKit imports
from livekit import rtc
from livekit.rtc import DataPacket

# ROS imports
import rospy
import actionlib
from std_msgs.msg import String
from geometry_msgs.msg import Twist
from sensor_msgs.msg import Image
from move_base_msgs.msg import MoveBaseAction, MoveBaseGoal
from your_robot_msgs.srv import RobotService, RobotServiceRequest
from cv_bridge import CvBridge


class CommandType(Enum):
    """命令类型枚举"""
    TOPIC_PUBLISH = "topic_publish"
    SERVICE_CALL = "service_call" 
    ACTION_CALL = "action_call"
    ACTION_CANCEL = "action_cancel"
    ACTION_FEEDBACK = "action_feedback"


class ResponseType(Enum):
    """响应类型枚举"""
    ACK = "ack"
    SERVICE_RESPONSE = "service_response"
    ACTION_RESULT = "action_result"
    ACTION_FEEDBACK = "action_feedback"
    ERROR = "error"


@dataclass
class CommandMessage:
    """统一命令消息格式"""
    id: str
    type: CommandType
    topic_name: str
    message_type: str
    data: Dict[str, Any]
    timestamp: float
    timeout: Optional[float] = None
    
    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result['type'] = self.type.value
        return result
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'CommandMessage':
        data['type'] = CommandType(data['type'])
        return cls(**data)


@dataclass
class ResponseMessage:
    """统一响应消息格式"""
    id: str
    type: ResponseType
    success: bool
    data: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    timestamp: float = None
    
    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = time.time()
    
    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result['type'] = self.type.value
        return result


class LiveKitROSBridge:
    """LiveKit与ROS的统一桥接器"""
    
    def __init__(self, room: rtc.Room):
        self.room = room
        self.bridge = CvBridge()
        
        # ROS相关
        self.publishers: Dict[str, rospy.Publisher] = {}
        self.service_clients: Dict[str, rospy.ServiceProxy] = {}
        self.action_clients: Dict[str, actionlib.SimpleActionClient] = {}
        
        # 等待响应的映射
        self.pending_requests: Dict[str, threading.Event] = {}
        self.responses: Dict[str, ResponseMessage] = {}
        
        # 注册LiveKit事件
        self.room.on("data_received", self.on_data_received)
        
        # 启动图像发布线程
        self.image_thread = threading.Thread(target=self.image_publisher_loop, daemon=True)
        self.image_thread.start()
    
    async def on_data_received(self, data: rtc.DataPacket, participant: rtc.RemoteParticipant):
        """接收LiveKit数据包"""
        try:
            # 解析命令
            command_data = json.loads(data.data.decode('utf-8'))
            command = CommandMessage.from_dict(command_data)
            
            # 在新线程中处理命令，避免阻塞
            threading.Thread(
                target=self.process_command, 
                args=(command,), 
                daemon=True
            ).start()
            
        except Exception as e:
            await self.send_error_response("", f"解析命令失败: {e}")
    
    def process_command(self, command: CommandMessage):
        """处理统一命令"""
        try:
            if command.type == CommandType.TOPIC_PUBLISH:
                self.handle_topic_publish(command)
                
            elif command.type == CommandType.SERVICE_CALL:
                self.handle_service_call(command)
                
            elif command.type == CommandType.ACTION_CALL:
                self.handle_action_call(command)
                
            elif command.type == CommandType.ACTION_CANCEL:
                self.handle_action_cancel(command)
                
        except Exception as e:
            asyncio.run_coroutine_threadsafe(
                self.send_error_response(command.id, str(e)),
                asyncio.get_event_loop()
            )
    
    def handle_topic_publish(self, command: CommandMessage):
        """处理Topic发布"""
        topic_name = command.topic_name
        
        # 获取或创建Publisher
        if topic_name not in self.publishers:
            # 动态创建Publisher（需要根据message_type导入对应的消息类型）
            msg_class = self.get_message_class(command.message_type)
            self.publishers[topic_name] = rospy.Publisher(
                topic_name, msg_class, queue_size=10
            )
            rospy.sleep(0.1)  # 等待连接建立
        
        # 构造并发布消息
        msg = self.construct_message(command.message_type, command.data)
        self.publishers[topic_name].publish(msg)
        
        # 发送ACK响应
        response = ResponseMessage(
            id=command.id,
            type=ResponseType.ACK,
            success=True,
            data={"published_to": topic_name}
        )
        asyncio.run_coroutine_threadsafe(
            self.send_response(response),
            asyncio.get_event_loop()
        )
    
    def handle_service_call(self, command: CommandMessage):
        """处理Service调用"""
        service_name = command.topic_name  # 这里复用topic_name字段作为service_name
        
        # 获取或创建Service Client
        if service_name not in self.service_clients:
            srv_class = self.get_service_class(command.message_type)
            rospy.wait_for_service(service_name, timeout=5.0)
            self.service_clients[service_name] = rospy.ServiceProxy(service_name, srv_class)
        
        try:
            # 构造服务请求
            request = self.construct_service_request(command.message_type, command.data)
            
            # 调用服务
            response_data = self.service_clients[service_name](request)
            
            # 发送服务响应
            response = ResponseMessage(
                id=command.id,
                type=ResponseType.SERVICE_RESPONSE,
                success=True,
                data=self.extract_service_response(response_data)
            )
            
        except Exception as e:
            response = ResponseMessage(
                id=command.id,
                type=ResponseType.ERROR,
                success=False,
                error_message=str(e)
            )
        
        asyncio.run_coroutine_threadsafe(
            self.send_response(response),
            asyncio.get_event_loop()
        )
    
    def handle_action_call(self, command: CommandMessage):
        """处理Action调用"""
        action_name = command.topic_name
        
        # 获取或创建Action Client
        if action_name not in self.action_clients:
            action_class = self.get_action_class(command.message_type)
            self.action_clients[action_name] = actionlib.SimpleActionClient(
                action_name, action_class
            )
            self.action_clients[action_name].wait_for_server(timeout=rospy.Duration(5.0))
        
        # 构造Action Goal
        goal = self.construct_action_goal(command.message_type, command.data)
        
        # 设置回调函数
        def done_callback(state, result):
            response = ResponseMessage(
                id=command.id,
                type=ResponseType.ACTION_RESULT,
                success=(state == actionlib.GoalStatus.SUCCEEDED),
                data={
                    "state": state,
                    "result": self.extract_action_result(result) if result else None
                }
            )
            asyncio.run_coroutine_threadsafe(
                self.send_response(response),
                asyncio.get_event_loop()
            )
        
        def feedback_callback(feedback):
            response = ResponseMessage(
                id=command.id,
                type=ResponseType.ACTION_FEEDBACK,
                success=True,
                data=self.extract_action_feedback(feedback)
            )
            asyncio.run_coroutine_threadsafe(
                self.send_response(response),
                asyncio.get_event_loop()
            )
        
        # 发送Goal
        self.action_clients[action_name].send_goal(
            goal,
            done_cb=done_callback,
            feedback_cb=feedback_callback
        )
        
        # 立即发送ACK
        ack_response = ResponseMessage(
            id=command.id,
            type=ResponseType.ACK,
            success=True,
            data={"action_started": action_name}
        )
        asyncio.run_coroutine_threadsafe(
            self.send_response(ack_response),
            asyncio.get_event_loop()
        )
    
    def handle_action_cancel(self, command: CommandMessage):
        """处理Action取消"""
        action_name = command.topic_name
        
        if action_name in self.action_clients:
            self.action_clients[action_name].cancel_all_goals()
            
            response = ResponseMessage(
                id=command.id,
                type=ResponseType.ACK,
                success=True,
                data={"action_cancelled": action_name}
            )
        else:
            response = ResponseMessage(
                id=command.id,
                type=ResponseType.ERROR,
                success=False,
                error_message=f"Action client {action_name} not found"
            )
        
        asyncio.run_coroutine_threadsafe(
            self.send_response(response),
            asyncio.get_event_loop()
        )
    
    async def send_response(self, response: ResponseMessage):
        """发送响应到LiveKit"""
        try:
            data_str = json.dumps(response.to_dict())
            data_packet = DataPacket(data_str.encode('utf-8'))
            await self.room.local_participant.publish_data(data_packet)
        except Exception as e:
            rospy.logerr(f"发送响应失败: {e}")
    
    async def send_error_response(self, command_id: str, error_message: str):
        """发送错误响应"""
        response = ResponseMessage(
            id=command_id,
            type=ResponseType.ERROR,
            success=False,
            error_message=error_message
        )
        await self.send_response(response)
    
    def get_message_class(self, message_type: str):
        """根据消息类型字符串获取消息类"""
        # 这里需要实现消息类型到Python类的映射
        mapping = {
            "geometry_msgs/Twist": Twist,
            "std_msgs/String": String,
            # 添加更多映射...
        }
        return mapping.get(message_type)
    
    def get_service_class(self, service_type: str):
        """根据服务类型字符串获取服务类"""
        # 实现服务类型映射
        mapping = {
            "your_robot_msgs/RobotService": RobotService,
            # 添加更多映射...
        }
        return mapping.get(service_type)
    
    def get_action_class(self, action_type: str):
        """根据Action类型字符串获取Action类"""
        mapping = {
            "move_base_msgs/MoveBaseAction": MoveBaseAction,
            # 添加更多映射...
        }
        return mapping.get(action_type)
    
    def construct_message(self, message_type: str, data: Dict[str, Any]):
        """构造ROS消息"""
        if message_type == "geometry_msgs/Twist":
            msg = Twist()
            msg.linear.x = data.get('linear_x', 0.0)
            msg.linear.y = data.get('linear_y', 0.0)
            msg.linear.z = data.get('linear_z', 0.0)
            msg.angular.x = data.get('angular_x', 0.0)
            msg.angular.y = data.get('angular_y', 0.0)
            msg.angular.z = data.get('angular_z', 0.0)
            return msg
        elif message_type == "std_msgs/String":
            msg = String()
            msg.data = data.get('data', '')
            return msg
        # 添加更多消息类型构造...
        
    def construct_service_request(self, service_type: str, data: Dict[str, Any]):
        """构造服务请求"""
        # 根据服务类型构造请求
        if service_type == "your_robot_msgs/RobotService":
            request = RobotServiceRequest()
            request.command = data.get('command', '')
            request.parameters = data.get('parameters', [])
            return request
        # 添加更多服务类型...
    
    def construct_action_goal(self, action_type: str, data: Dict[str, Any]):
        """构造Action Goal"""
        if action_type == "move_base_msgs/MoveBaseAction":
            goal = MoveBaseGoal()
            goal.target_pose.header.frame_id = data.get('frame_id', 'map')
            goal.target_pose.pose.position.x = data.get('x', 0.0)
            goal.target_pose.pose.position.y = data.get('y', 0.0)
            goal.target_pose.pose.orientation.z = data.get('yaw', 0.0)
            return goal
        # 添加更多Action类型...
    
    def extract_service_response(self, response) -> Dict[str, Any]:
        """提取服务响应数据"""
        # 根据响应类型提取数据
        return {"result": str(response)}
    
    def extract_action_result(self, result) -> Dict[str, Any]:
        """提取Action结果数据"""
        return {"result": str(result)}
    
    def extract_action_feedback(self, feedback) -> Dict[str, Any]:
        """提取Action反馈数据"""
        return {"feedback": str(feedback)}
    
    def image_publisher_loop(self):
        """图像发布循环"""
        def image_callback(msg: Image):
            try:
                # 转换图像并发送到LiveKit
                cv_image = self.bridge.imgmsg_to_cv2(msg, "bgr8")
                # 这里需要实现图像发送到LiveKit的逻辑
                pass
            except Exception as e:
                rospy.logerr(f"图像处理错误: {e}")
        
        # 订阅图像话题
        rospy.Subscriber("/camera/image_raw", Image, image_callback)


# 客户端（VR端）使用示例
class VRController:
    """VR控制器示例"""
    
    def __init__(self, room: rtc.Room):
        self.room = room
        self.pending_commands: Dict[str, CommandMessage] = {}
    
    async def send_movement_command(self, linear_x: float, angular_z: float):
        """发送运动控制命令（Topic方式）"""
        command = CommandMessage(
            id=str(uuid.uuid4()),
            type=CommandType.TOPIC_PUBLISH,
            topic_name="/cmd_vel",
            message_type="geometry_msgs/Twist",
            data={
                "linear_x": linear_x,
                "angular_z": angular_z
            },
            timestamp=time.time()
        )
        
        await self.send_command(command)
    
    async def call_navigation_service(self, target_x: float, target_y: float):
        """调用导航服务（Service方式）"""
        command = CommandMessage(
            id=str(uuid.uuid4()),
            type=CommandType.SERVICE_CALL,
            topic_name="/navigate_to_goal",
            message_type="your_robot_msgs/NavigationService",
            data={
                "target_x": target_x,
                "target_y": target_y
            },
            timestamp=time.time(),
            timeout=10.0
        )
        
        return await self.send_command_with_response(command)
    
    async def start_navigation_action(self, x: float, y: float, yaw: float):
        """启动导航Action（Action方式）"""
        command = CommandMessage(
            id=str(uuid.uuid4()),
            type=CommandType.ACTION_CALL,
            topic_name="/move_base",
            message_type="move_base_msgs/MoveBaseAction",
            data={
                "frame_id": "map",
                "x": x,
                "y": y,
                "yaw": yaw
            },
            timestamp=time.time()
        )
        
        await self.send_command(command)
    
    async def send_command(self, command: CommandMessage):
        """发送命令（无需等待响应）"""
        data_str = json.dumps(command.to_dict())
        data_packet = DataPacket(data_str.encode('utf-8'))
        await self.room.local_participant.publish_data(data_packet)
    
    async def send_command_with_response(self, command: CommandMessage, timeout: float = 5.0):
        """发送命令并等待响应"""
        self.pending_commands[command.id] = command
        await self.send_command(command)
        
        # 等待响应（简化版，实际需要更复杂的超时处理）
        start_time = time.time()
        while time.time() - start_time < timeout:
            if command.id in self.responses:
                response = self.responses.pop(command.id)
                self.pending_commands.pop(command.id, None)
                return response
            await asyncio.sleep(0.1)
        
        # 超时
        self.pending_commands.pop(command.id, None)
        raise TimeoutError(f"Command {command.id} timed out")


# 使用示例
async def main():
    """主函数示例"""
    # 初始化ROS
    rospy.init_node('vr_robot_controller')
    
    # 连接LiveKit
    room = rtc.Room()
    await room.connect("wss://your-livekit-server.com", "your-token")
    
    # 创建桥接器
    bridge = LiveKitROSBridge(room)
    
    # VR控制器示例
    vr_controller = VRController(room)
    
    # 发送运动命令
    await vr_controller.send_movement_command(1.0, 0.5)
    
    # 调用服务
    try:
        response = await vr_controller.call_navigation_service(2.0, 3.0)
        print(f"服务响应: {response}")
    except TimeoutError:
        print("服务调用超时")
    
    # 启动Action
    await vr_controller.start_navigation_action(5.0, 2.0, 1.57)


if __name__ == "__main__":
    asyncio.run(main())
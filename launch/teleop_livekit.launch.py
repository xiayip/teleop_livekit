import os

from ament_index_python.packages import get_package_share_directory

import launch
from launch.actions import ExecuteProcess
from launch_ros.actions import ComposableNodeContainer
from launch_ros.descriptions import ComposableNode
from launch_ros.actions import Node


def generate_launch_description():
    # RealSense
    orbbec_config_file_path = os.path.join(
        get_package_share_directory('teleop_livekit'),
        'config', 'orbbec.yaml'
    )

    orbbec_node = ComposableNode(
        package='orbbec_camera',
        plugin='orbbec_camera::OBCameraNodeDriver',
        parameters=[orbbec_config_file_path],
        remappings=[]
    )

    container = ComposableNodeContainer(
        name='encoder_container',
        namespace='encoder',
        package='rclcpp_components',
        executable='component_container_mt',
        composable_node_descriptions=[orbbec_node],
        output='screen'
    )

    # LiveKit
    livekit_node = Node(
        package='teleop_livekit',
        executable='teleop_livekit',
        name='livekit_image_publisher',
        output='screen',
        remappings=[
            ('/image_raw', '/color/image_raw'),
        ]
    )

    return (launch.LaunchDescription([container] + [livekit_node]))

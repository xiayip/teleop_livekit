import os

from ament_index_python.packages import get_package_share_directory

import launch
from launch.actions import ExecuteProcess
from launch_ros.actions import ComposableNodeContainer
from launch_ros.descriptions import ComposableNode
from launch_ros.actions import Node
from launch.substitutions import PathJoinSubstitution
from launch_ros.substitutions import FindPackageShare

def generate_launch_description():
    # RealSense
    orbbec_config_file_path = os.path.join(
        get_package_share_directory('teleop_livekit'),
        'config', 'orbbec.yaml'
    )
    # wrist camera stream
    orbbec_node = ComposableNode(
        package='orbbec_camera',
        plugin='orbbec_camera::OBCameraNodeDriver',
        parameters=[orbbec_config_file_path],
        remappings=[]
    )
    rgb_image_container = ComposableNodeContainer(
        name='encoder_container',
        namespace='encoder',
        package='rclcpp_components',
        executable='component_container_mt',
        composable_node_descriptions=[orbbec_node],
        output='screen'
    )
    
    # rgb point cloud stream
    config_file_path = PathJoinSubstitution([FindPackageShare('odin1_ros2_driver'), 'config', 'control_command.yaml'])
    odin1_node = ComposableNode(
        package='odin1_ros2_driver',
        plugin='odin1_ros2_driver::Odin1Driver',
        name='odin1_ros2_driver',
        parameters=[config_file_path],
        extra_arguments=[{'use_intra_process_comms': True}],
    )
    # Incremental map builder
    incremental_map_node = ComposableNode(
        package='odin1_ros2_driver',
        plugin='odin1_ros2_driver::IncrementalMapNode',
        name='incremental_map_node',
        parameters=[{
            'voxel_leaf_size': 0.01,           # 1cm voxel grid
            'octree_resolution': 0.02,         # 2cm octree resolution
            'novelty_threshold': 0.02,         # 2cm radius for novelty check
            'max_accumulated_points': 1000000, # 1M point limit
        }],
        extra_arguments=[{'use_intra_process_comms': True}],
    )
    point_compression_node = ComposableNode(
        package='pointcloud_compressor',
        plugin='PointCloudCompressorNode',
        name='pointcloud_compressor_node',
        parameters=[{
            'input_topic': 'odin1/cloud_incremental', 
            'output_topic': 'compressed_pointcloud',
            'compression_level': 6,  # default compression level
            'quantization_bits': 16  # default quantization bits
        }],
        extra_arguments=[{'use_intra_process_comms': True}],
    )
    point_cloud_container = ComposableNodeContainer(
        name='odin1_container',
        namespace='',
        package='rclcpp_components',
        executable='component_container',
        composable_node_descriptions=[
            odin1_node,
            incremental_map_node,
            point_compression_node,
        ],
        output='screen',
    )
    # LiveKit
    livekit_node = Node(
        package='teleop_livekit',
        executable='teleop_bridge',
        name='livekit_image_publisher',
        output='screen',
        remappings=[
            ('/image_raw', '/color/image_raw'),
        ]
    )

    return (launch.LaunchDescription(
                                     [rgb_image_container] + 
                                     [point_cloud_container] + 
                                     [livekit_node])
                                     )

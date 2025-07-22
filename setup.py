from setuptools import find_packages, setup

package_name = 'teleop_livekit'

setup(
    name=package_name,
    version='0.0.0',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages',
            ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
        ('share/' + package_name + '/config', ['config/orbbec.yaml']),
        ('share/' + package_name + '/config', ['config/livekit.yaml']),
        ('share/' + package_name + '/config', ['config/camera.yaml']),
        ('share/' + package_name + '/launch', ['launch/teleop_livekit.launch.py']),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='nvidia',
    maintainer_email='yipeng.xia@hotmail.com',
    description='TODO: Package description',
    license='TODO: License declaration',
    tests_require=['pytest'],
    entry_points={
        'console_scripts': [
            'teleop_livekit = teleop_livekit.teleop_livekit:run',
        ],
    },
)

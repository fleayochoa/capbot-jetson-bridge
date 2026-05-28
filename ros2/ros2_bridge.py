"""Bridge ROS 2 dentro del servicio Jetson.

Diseno (Opcion A: integracion al servicio existente):

    +-----------------------------------------------------------+
    |   asyncio loop (servicio Jetson)                          |
    |                                                           |
    |   bus (EventBus)                                          |
    |    |  Ev.TELEMETRY (desde Esp32Link)                      |
    |    |                                                      |
    |    +--> bridge.on_telemetry()  --rclpy publish-->         |
    |    |                              /odom, /imu/data,       |
    |    |                              /joint_states           |
    |    |                                                      |
    |    +<-- bridge.on_cmd_vel()   <--rclpy subscription--     |
    |    |   (via call_soon_threadsafe)                         |
    |    |                                                      |
    |    +--> Ev.CMD_VEL --> Esp32Link --> serial (VEL_CMD)     |
    |                                                           |
    +-----------------------------------------------------------+

Por que esto y no rclpy.spin() directo:
  - rclpy.spin() bloquea el thread, incompatible con el asyncio loop.
  - Las subscriptions ROS llaman callbacks en el thread del executor.
    Para tocar el bus (que vive en asyncio) usamos call_soon_threadsafe.
  - Las publicaciones ROS las hacemos desde el callback del bus, que
    corre en asyncio. rclpy.publisher.publish() es seguro de llamar
    desde cualquier thread.

Lifecycle:
  - run_ros2_bridge() inicializa rclpy, crea el nodo, lanza el executor
    en un thread, suscribe el callback del bus, y espera al stop_event.
  - Al stop: cancela executor, destruye nodo, rclpy.shutdown().
"""
import asyncio
import logging
import math
import threading
import time
from typing import Optional

try:
    import rclpy
    from rclpy.node import Node
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
    from geometry_msgs.msg import Twist, TransformStamped
    from nav_msgs.msg import Odometry as OdometryMsg
    from sensor_msgs.msg import Imu, JointState
    from std_msgs.msg import Header
    from tf2_ros import TransformBroadcaster
    _ROS_AVAILABLE = True
except ImportError:  # pragma: no cover
    _ROS_AVAILABLE = False

from core.bus import Ev, bus

log = logging.getLogger(__name__)


# ------------------------------------------------------------
# Parametros del robot (deben coincidir con xacro / firmware)
# ------------------------------------------------------------
WHEEL_DIAMETER = 0.067   # m, debe ser igual al de Odometry.h
WHEEL_RADIUS = WHEEL_DIAMETER / 2.0
WHEEL_SEPARATION = 0.226  # m, debe ser igual al de gazebo_control.xacro
ENCODER_CPR = 910.0       # cuentas por revolucion, ver Odometry.h

# Frames TF
ODOM_FRAME = "odom"
BASE_FRAME = "base_link"
IMU_FRAME  = "imu_link"

# Joint names del URDF
LEFT_WHEEL_JOINT  = "left_wheel_joint"
RIGHT_WHEEL_JOINT = "right_wheel_joint"


class Esp32RosBridge(Node):
    """Nodo ROS 2 que traduce entre topics y el bus interno."""

    def __init__(self, asyncio_loop: asyncio.AbstractEventLoop,
                 publish_odom_tf: bool = False):
        super().__init__("esp32_bridge")
        self._loop = asyncio_loop
        self._publish_odom_tf = publish_odom_tf

        qos_sensor = QoSProfile(
            depth=10,
            reliability=ReliabilityPolicy.BEST_EFFORT,
            history=HistoryPolicy.KEEP_LAST,
        )
        qos_reliable = QoSProfile(
            depth=10,
            reliability=ReliabilityPolicy.RELIABLE,
            history=HistoryPolicy.KEEP_LAST,
        )

        # Subscriptions
        self._sub_cmd_vel = self.create_subscription(
            Twist, "cmd_vel", self._on_cmd_vel_ros, qos_reliable
        )

        # Publishers
        self._pub_odom = self.create_publisher(OdometryMsg, "odom", qos_reliable)
        self._pub_imu  = self.create_publisher(Imu,         "imu/data", qos_sensor)
        self._pub_js   = self.create_publisher(JointState,  "joint_states", qos_reliable)

        # TF (solo si publish_odom_tf=True; con EKF este se desactiva)
        self._tf_br = TransformBroadcaster(self) if publish_odom_tf else None

        # Estado para joint_states (integramos posicion de rueda a partir de cps)
        self._left_wheel_pos = 0.0
        self._right_wheel_pos = 0.0
        self._last_telemetry_t_ms = 0  # uptime_ms del ESP32

        # Para imu/data: covarianzas tipicas BNO055
        # En diagonal: x, y, z. Cero o -1 = unknown segun convencion ROS.
        self._orientation_cov = [
            0.01, 0.0,  0.0,
            0.0,  0.01, 0.0,
            0.0,  0.0,  0.01,
        ]
        self._angular_velocity_cov = [
            0.001, 0.0,   0.0,
            0.0,   0.001, 0.0,
            0.0,   0.0,   0.001,
        ]
        self._linear_acceleration_cov = [
            0.04, 0.0,  0.0,
            0.0,  0.04, 0.0,
            0.0,  0.0,  0.04,
        ]

        self.get_logger().info("esp32_bridge ready (publish_odom_tf=%s)" % publish_odom_tf)

    # ---------------------------------------------------------
    # ROS subscriber -> bus
    # ---------------------------------------------------------
    def _on_cmd_vel_ros(self, msg: "Twist") -> None:
        # Se ejecuta en el thread del executor ROS. Saltamos al asyncio loop
        # para tocar el bus de manera segura.
        v_mms    = int(round(msg.linear.x  * 1000.0))
        w_mrad_s = int(round(msg.angular.z * 1000.0))
        data = {"v_mms": v_mms, "w_mrads": w_mrad_s}
        self._loop.call_soon_threadsafe(bus.emit, Ev.CMD_VEL, data)

    # ---------------------------------------------------------
    # bus.TELEMETRY -> ROS publishers (corre en asyncio loop)
    # ---------------------------------------------------------
    def on_telemetry(self, data) -> None:
        if not isinstance(data, dict):
            return
        try:
            self._publish_from_telemetry(data)
        except Exception:
            log.exception("Error publicando telemetria a ROS")

    def _publish_from_telemetry(self, t: dict) -> None:
        now = self.get_clock().now().to_msg()

        odo = t.get("odo") or {}
        imu = t.get("imu") or {}
        enc = t.get("enc") or {}

        # ---- /odom ----
        msg = OdometryMsg()
        msg.header = Header()
        msg.header.stamp = now
        msg.header.frame_id = ODOM_FRAME
        msg.child_frame_id = BASE_FRAME
        msg.pose.pose.position.x = float(odo.get("x", 0.0))
        msg.pose.pose.position.y = float(odo.get("y", 0.0))
        msg.pose.pose.position.z = 0.0
        # theta en grados -> quaternion (rot Z)
        theta_rad = math.radians(float(odo.get("th", 0.0)))
        msg.pose.pose.orientation.z = math.sin(theta_rad * 0.5)
        msg.pose.pose.orientation.w = math.cos(theta_rad * 0.5)
        msg.twist.twist.linear.x = float(odo.get("v", 0.0))
        # omega en grados/s -> rad/s
        msg.twist.twist.angular.z = math.radians(float(odo.get("om", 0.0)))

        # Covarianzas conservadoras: la odometria pura de ruedas es ruidosa.
        # Solo poblamos las diagonales que el EKF lee.
        cov = list(msg.pose.covariance)
        cov[0]  = 0.02   # x
        cov[7]  = 0.02   # y
        cov[35] = 0.05   # yaw
        msg.pose.covariance = tuple(cov)
        twist_cov = list(msg.twist.covariance)
        twist_cov[0]  = 0.01
        twist_cov[35] = 0.02
        msg.twist.covariance = tuple(twist_cov)

        self._pub_odom.publish(msg)

        if self._tf_br is not None:
            tfm = TransformStamped()
            tfm.header.stamp = now
            tfm.header.frame_id = ODOM_FRAME
            tfm.child_frame_id = BASE_FRAME
            tfm.transform.translation.x = msg.pose.pose.position.x
            tfm.transform.translation.y = msg.pose.pose.position.y
            tfm.transform.rotation = msg.pose.pose.orientation
            self._tf_br.sendTransform(tfm)

        # ---- /imu/data ----
        if imu:
            imu_msg = Imu()
            imu_msg.header.stamp = now
            imu_msg.header.frame_id = IMU_FRAME
            imu_msg.orientation.w = float(imu.get("qw", 1.0))
            imu_msg.orientation.x = float(imu.get("qx", 0.0))
            imu_msg.orientation.y = float(imu.get("qy", 0.0))
            imu_msg.orientation.z = float(imu.get("qz", 0.0))
            imu_msg.orientation_covariance = tuple(self._orientation_cov)

            # Gyro: ESP32 lo manda en deg/s -> rad/s
            imu_msg.angular_velocity.x = math.radians(float(imu.get("gx", 0.0)))
            imu_msg.angular_velocity.y = math.radians(float(imu.get("gy", 0.0)))
            imu_msg.angular_velocity.z = math.radians(float(imu.get("gz", 0.0)))
            imu_msg.angular_velocity_covariance = tuple(self._angular_velocity_cov)

            # Aceleracion lineal: usar VECTOR_LINEARACCEL (sin gravedad), que
            # es lo que el EKF espera para integrar. Si no esta en el payload
            # caemos en VECTOR_ACCELEROMETER (con gravedad), peor pero algo.
            ax = imu.get("lx")
            ay = imu.get("ly")
            az = imu.get("lz")
            if ax is None or ay is None or az is None:
                ax = imu.get("ax", 0.0)
                ay = imu.get("ay", 0.0)
                az = imu.get("az", 0.0)
            imu_msg.linear_acceleration.x = float(ax)
            imu_msg.linear_acceleration.y = float(ay)
            imu_msg.linear_acceleration.z = float(az)
            imu_msg.linear_acceleration_covariance = tuple(self._linear_acceleration_cov)
            self._pub_imu.publish(imu_msg)

        # ---- /joint_states ----
        # Integramos la posicion angular de cada rueda a partir de cps.
        t_ms = int(t.get("t", 0))
        if self._last_telemetry_t_ms == 0 or t_ms < self._last_telemetry_t_ms:
            self._last_telemetry_t_ms = t_ms
        else:
            dt = (t_ms - self._last_telemetry_t_ms) / 1000.0
            self._last_telemetry_t_ms = t_ms
            if dt > 0:
                # cps -> rad/s en la rueda
                cps_to_radps = (2.0 * math.pi) / ENCODER_CPR
                wl = float(enc.get("lc", 0.0)) * cps_to_radps
                wr = float(enc.get("rc", 0.0)) * cps_to_radps
                self._left_wheel_pos  += wl * dt
                self._right_wheel_pos += wr * dt

        js = JointState()
        js.header.stamp = now
        js.name = [LEFT_WHEEL_JOINT, RIGHT_WHEEL_JOINT]
        js.position = [self._left_wheel_pos, self._right_wheel_pos]
        # velocidades en rad/s
        cps_to_radps = (2.0 * math.pi) / ENCODER_CPR
        js.velocity = [
            float(enc.get("lc", 0.0)) * cps_to_radps,
            float(enc.get("rc", 0.0)) * cps_to_radps,
        ]
        self._pub_js.publish(js)


# ------------------------------------------------------------
# Runner asyncio
# ------------------------------------------------------------
async def run_ros2_bridge(stop_event: asyncio.Event,
                          loop: asyncio.AbstractEventLoop,
                          publish_odom_tf: bool = False) -> None:
    if not _ROS_AVAILABLE:
        log.error("rclpy no disponible; bridge ROS deshabilitado")
        await stop_event.wait()
        return

    rclpy.init()
    node = Esp32RosBridge(asyncio_loop=loop, publish_odom_tf=publish_odom_tf)

    # Subscribir al bus para reemitir telemetria a ROS.
    # El callback corre en el asyncio loop, igual que el bus.emit.
    bus.on(Ev.TELEMETRY, node.on_telemetry)

    # Executor ROS en thread separado para no bloquear asyncio.
    executor = SingleThreadedExecutor()
    executor.add_node(node)

    def spin_target():
        try:
            executor.spin()
        except Exception:
            log.exception("ROS executor crasheo")

    spin_thread = threading.Thread(target=spin_target, name="ros-executor", daemon=True)
    spin_thread.start()
    log.info("ros2_bridge arrancado")

    try:
        await stop_event.wait()
    finally:
        log.info("Cerrando ros2_bridge")
        executor.shutdown()
        node.destroy_node()
        try:
            rclpy.shutdown()
        except Exception:
            pass
        spin_thread.join(timeout=2.0)

import cv2
import socket
import struct
import time
import numpy as np
import threading
# import json # 未使用のためコメントアウト

class CameraClient:
    def __init__(self, server_ip, server_port=9999):
        self.server_ip = server_ip
        self.server_port = server_port
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.loop_running = True
        self.__loop_thread_send = None
        self.__loop_thread_recv = None
        
        # __send_data は send_loop 内で生成されるため、ここでは None で問題ない
        # self.__send_data = None 
        
        self.__recv_data_lock = threading.Lock() # 受信データ保護用ロック
        self.__requested_camera_num = 1 # サーバーから要求されたカメラ番号を保持
        
        # カメラ初期化はconnect前に必要なので、ここで実施
        self.cameras = Camera.initialize_cameras() 
        
    def connect(self):
        try:
            self.socket.connect((self.server_ip, self.server_port))
            print(f"[INFO] サーバ ({self.server_ip}:{self.server_port}) に接続しました。")
            self.__loop_thread_send = threading.Thread(target=self.send_loop, daemon=True) # daemon=Trueを追加
            self.__loop_thread_recv = threading.Thread(target=self.receive_loop, daemon=True) # daemon=Trueを追加
            self.__loop_thread_send.start()
            self.__loop_thread_recv.start()

        except Exception as e:
            print(f"[ERROR] サーバ接続失敗: {e}")
            self.loop_running = False # 接続失敗時はループを停止
            raise
    
    def wait_for_termination(self):
        # メインスレッドが終了するまで待機
        try:
            while self.loop_running:
                time.sleep(1) 
        except KeyboardInterrupt:
            print("[INFO] 停止要求を受け取りました。")
        finally:
            self.loop_running = False
            if self.__loop_thread_send and self.__loop_thread_send.is_alive():
                self.__loop_thread_send.join(timeout=1)
            if self.__loop_thread_recv and self.__loop_thread_recv.is_alive():
                self.__loop_thread_recv.join(timeout=1)
            self.socket.close()
            for cam_obj in self.cameras: # cameraオブジェクトからcapを解放
                if cam_obj._Camera__camera.isOpened():
                    cam_obj._Camera__camera.release()
            print("[INFO] クライアント終了。")

    def send_loop(self):
        try:
            while self.loop_running:
                try:
                    # サーバーから要求されたカメラ番号を取得
                    with self.__recv_data_lock:
                        current_request_num = self.__requested_camera_num

                    # 現在要求されているカメラの画像データを取得
                    target_camera_data = None
                    for cam_obj in self.cameras:
                        if cam_obj.camera_num == current_request_num:
                            target_camera_data = cam_obj.capture_and_encode()
                            break
                    
                    if target_camera_data:
                        # まずタイムスタンプを送信
                        self.__timestamp_send()
                        # 次にカメラデータを送信
                        self.socket.sendall(target_camera_data)
                        # print(f"[DEBUG] カメラ{current_request_num}のデータを送信しました。")
                    else:
                        # 要求されたカメラが見つからない場合は、デフォルトのカメラ1を試す
                        # print(f"[WARN] カメラ{current_request_num}のデータが見つかりません。デフォルトのカメラ1を試します。")
                        with self.__recv_data_lock:
                            self.__requested_camera_num = 1 # デフォルトに戻す
                        time.sleep(0.1) # 無限ループにならないように待機
                        print()
                        continue
                    
                    time.sleep(0.01) # 送信レートの調整

                except (ConnectionResetError, BrokenPipeError):
                    print("[ERROR] サーバとの接続が切断されました。送信ループを終了します。")
                    self.loop_running = False
                    break
                except socket.error as se:
                    print(f"[ERROR] ソケットエラー (送信ループ): {se}")
                    self.loop_running = False
                    break
                except Exception as e:
                    print(f"[ERROR] 送信ループ中にエラー発生: {e}")
                    self.loop_running = False
                    break
        finally:
            pass # 終了処理はwait_for_terminationで行う

    def receive_loop(self):
        try:
            while self.loop_running:
                try:
                    # サーバから要求されたカメラ番号を1バイトで受信
                    received_byte = self.socket.recv(1)
                    if not received_byte:
                        print("[INFO] サーバが切断されました。受信ループを終了します。")
                        self.loop_running = False
                        break
                    
                    # 受信したバイトを整数に変換し、要求カメラ番号を更新
                    requested_cam_id = struct.unpack('>B', received_byte)[0]
                    with self.__recv_data_lock:
                        self.__requested_camera_num = requested_cam_id
                    # print(f"[DEBUG] サーバからカメラ{requested_cam_id}の要求を受信しました。")
                    
                except (ConnectionResetError, BrokenPipeError):
                    print("[ERROR] サーバとの接続が切断されました。受信ループを終了します。")
                    self.loop_running = False
                    break
                except struct.error as se:
                    print(f"[ERROR] 受信データ形式エラー (受信ループ): {se}")
                    self.loop_running = False
                    break
                except socket.error as se:
                    print(f"[ERROR] ソケットエラー (受信ループ): {se}")
                    self.loop_running = False
                    break
                except Exception as e:
                    print(f"[ERROR] 受信ループ中にエラー発生: {e}")
                    self.loop_running = False
                    break
        finally:
            pass # 終了処理はwait_for_terminationで行う

    # send_data_setは不要になりました。send_loop内でカメラデータを直接取得するため。
    # def send_data_set(self, set_data):
    #     self.__send_data = set_data

    # recv_data_getは不要になりました。__requested_camera_numを直接参照するため。
    # def recv_data_get(self):
    #     return int(struct.unpack('>c',self.__recv_data)[0]) # struct.unpack('>c', self.__recv_data) は1バイトなのでint()でキャストできる

    def __timestamp_send(self):
        timestamp = time.time()
        # 't' (1バイト) + タイムスタンプ (8バイト、double)
        self.socket.sendall(struct.pack(">c", b't') + struct.pack('>d', timestamp))

class Camera: # クラス名を'camera'から'Camera'に変更 (PEP8準拠)
    def __init__(self, jpeg_quality, cap, camera_num):
        self.__camera = cap
        self.__jpeg_quality = jpeg_quality
        self.camera_num = camera_num
        self.__camera_data = None # キャプチャ＆エンコードされたデータ

    @staticmethod # initialize_camerasをクラスメソッドに変更
    def initialize_cameras(jpeg_quality=40): # デフォルト品質を60に設定
        cameras = []
        camera_num = 1
        for i in range(10):
            cap = cv2.VideoCapture(i, cv2.CAP_MSMF)
            if cap.isOpened():
                print(f"[INFO] カメラ {i} を初期化しました。")
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)  # 例: 幅を640ピクセルに設定
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480) # 例: 高さを480ピクセルに設定

                # ここでFPSを30に設定します
                cap.set(cv2.CAP_PROP_FPS, 30) 
                
                cameras.append(Camera(jpeg_quality=jpeg_quality, cap=cap, camera_num=camera_num))
                camera_num += 1
            else:
                # print(f"[WARN] カメラ {i} は利用できません。") # ログが多すぎる場合があるのでコメントアウト
                cap.release()
        if not cameras:
            raise RuntimeError("[ERROR] 有効なカメラが見つかりません。")
        return cameras
    
    # def camera_reset(): # 未使用かつ実装がないため削除
    #     pass

    def __capture_camera(self, cap):
        ret, frame = cap.read()
        if not ret:
            # print("[WARN] フレーム取得に失敗しました。") # ログが多すぎる場合があるのでコメントアウト
            return None
        return frame

    def __encode_data(self, frame):
        if frame is None:
            return None
        encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), self.__jpeg_quality]
        result, encimg = cv2.imencode('.jpg', frame, encode_param)
        if not result:
            # print("[WARN] JPEGエンコードに失敗しました。") # ログが多すぎる場合があるのでコメントアウト
            return None
        return encimg.tobytes()
    
    def capture_and_encode(self):
        # 'c' (1バイト) + データ長 (4バイト) + データ本体
        frame_data = self.__encode_data(self.__capture_camera(self.__camera))
        if frame_data:
            # struct.pack(">c", ord('c')) を struct.pack(">c", b'c') に修正
            self.__camera_data = struct.pack(">c", b'c') + struct.pack('>L', len(frame_data)) + frame_data
            return self.__camera_data
        return None

    # camera_data_getはsend_loop内で直接呼び出すため不要になりました。
    # def camera_data_get(self, check_num=None):
    #     if self.camera_num == check_num or None is check_num:
    #         self.capture_and_encode()
    #         return self.__camera_data
    #     else:
    #         return None

if __name__ == '__main__':
    client_camera = None # エラー時に参照できるように初期化
    try:
        client_camera = CameraClient(server_ip='192.168.6.100')
        client_camera.connect()
        # メインループはクライアントオブジェクトに任せる
        client_camera.wait_for_termination() 
        
    except RuntimeError as e:
        print(e) # カメラが見つからない場合など
    except Exception as e:
        print(f"[FATAL] クライアントの実行中に予期せぬエラーが発生しました: {e}")
        if client_camera:
            client_camera.loop_running = False # エラーが発生したらループを終了
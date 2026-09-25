'use strict';

var React = require('react-native');
var {
  AppRegistry,
  Text,
  View,
  ScrollView,
  AsyncStorage,
  ToastAndroid,
  Alert,
} = React;

import _ from 'lodash';
import {styles} from './Styles';

import {
  SetServerButton,
  ServerAddressInput,
  ControllerButton,
  PlayPauseButton,
  SleepButton,
  ShutdownButton,
} from './ButtonSets';

var naiveClient = React.createClass({
  getInitialState: function () {
    return {
      playState: false,
      connectedServer: false,
      serverInfo: {},
      serverAddress: '',
    };
  },

  componentDidMount: async function () {
    var value = await AsyncStorage.getItem('serverAddress');
    if (value) {
      this.setState({
        serverAddress: value
      });
      this._setServer(value);
    }
  },

  _togglePlayState: function () {
    if (this.state.connectedServer) {
      this.setState({
        playState: !this.state.playState
      });
      this._sendCommand('pause_play');
    } else {
      ToastAndroid.show('请先连接服务器', ToastAndroid.SHORT);
    }
  },

  _inputServerAddress: function (event) {
    this.setState({
      serverAddress: event
    });
  },

  _setServer: async function (addr) {
    var target = addr || this.state.serverAddress;
    if (target) {
      fetch('http://' + target + '/mobile_connect')
        .then((response) => response.json())
        .then((response) => {
          this.setState({
            serverInfo: response,
            connectedServer: true,
          });
          ToastAndroid.show('已成功连接电脑！', ToastAndroid.SHORT);
        })
        .catch(() => {
          this.setState({
            connectedServer: false
          });
          ToastAndroid.show('连接失败，请检查 IP 与网络', ToastAndroid.SHORT);
        });

      await AsyncStorage.setItem('serverAddress', target);
    }
  },

  _sendCommand: function (command) {
    if (this.state.connectedServer) {
      fetch('http://' + this.state.serverAddress + '?action=' + command)
        .catch(() => {
          ToastAndroid.show('指令发送异常', ToastAndroid.SHORT);
        });
    } else {
      ToastAndroid.show('请先连接服务器', ToastAndroid.SHORT);
    }
  },

  _confirmSleep: function () {
    Alert.alert(
      '一键睡眠',
      '确认让电脑进入睡眠模式吗？',
      [
        {text: '取消', style: 'cancel'},
        {
          text: '确定睡眠',
          onPress: () => {
            this._sendCommand('sleep');
            ToastAndroid.show('已发送睡眠指令', ToastAndroid.SHORT);
          }
        }
      ]
    );
  },

  _confirmShutdown: function () {
    Alert.alert(
      '安全关机',
      '确认要关闭电脑吗？电脑将在 5 秒内安全关机。',
      [
        {text: '取消', style: 'cancel'},
        {
          text: '确定关机',
          style: 'destructive',
          onPress: () => {
            this._sendCommand('shutdown');
            ToastAndroid.show('电脑将在 5 秒后关机', ToastAndroid.SHORT);
          }
        }
      ]
    );
  },

  render: function() {
    return (
      <ScrollView style={styles.scrollView}
                  contentContainerStyle={styles.container}>
        
        {/* 顶部标题与状态栏 */}
        <View style={styles.headerRow}>
          <Text style={styles.welcome}>音乐遥控器</Text>
          <View style={styles.connectedBadge}>
            <Text style={styles.connectedText}>
              {this.state.connectedServer ? '● 已连接' : '○ 未连接'}
            </Text>
          </View>
        </View>

        {/* 服务器地址输入区 */}
        <View style={styles.row}>
          <View style={styles.colNoAlign}>
            <ServerAddressInput 
              value={this.state.serverAddress} 
              onTextChange={(e) => this._inputServerAddress(e)} 
            />
          </View>
          <View style={styles.col}>
            <SetServerButton onPress={() => this._setServer()}/>
          </View>
        </View>

        {/* 切歌控制 */}
        <View style={styles.row}>
          <View style={styles.col}>
            <ControllerButton onPress={() => this._sendCommand('prev_song')}>
              <Text style={styles.buttonText}>⏮ 上一首</Text>
            </ControllerButton>
          </View>
          <View style={styles.col}>
            <ControllerButton onPress={() => this._sendCommand('next_song')}>
              <Text style={styles.buttonText}>⏭ 下一首</Text>
            </ControllerButton>
          </View>
        </View>

        {/* 播放/暂停大按键 */}
        <View style={styles.row}>
          <View style={styles.col}>
            <PlayPauseButton onPress={() => this._togglePlayState()}>
              <Text style={styles.playButtonText}>
                ⏯ 播放 / 暂停
              </Text>
            </PlayPauseButton>
          </View>
        </View>

        {/* 音量加减 */}
        <View style={styles.row}>
          <View style={styles.col}>
            <ControllerButton onPress={() => this._sendCommand('volume_down')}>
              <Text style={styles.buttonText}>🔉 音量 -</Text>
            </ControllerButton>
          </View>
          <View style={styles.col}>
            <ControllerButton onPress={() => this._sendCommand('volume_up')}>
              <Text style={styles.buttonText}>🔊 音量 +</Text>
            </ControllerButton>
          </View>
        </View>

        {/* 系统电源控制（睡眠与关机） */}
        <View style={styles.row}>
          <View style={styles.col}>
            <SleepButton onPress={() => this._confirmSleep()}>
              <Text style={styles.buttonText}>🌙 一键睡眠</Text>
            </SleepButton>
          </View>
          <View style={styles.col}>
            <ShutdownButton onPress={() => this._confirmShutdown()}>
              <Text style={styles.buttonText}>⛔ 安全关机</Text>
            </ShutdownButton>
          </View>
        </View>

        {/* 服务器状态卡片 */}
        <View style={styles.card}>
          <Text style={styles.cardTitle}>连接状态信息</Text>
          <Text style={styles.textLine}>
            运行状态: {this.state.connectedServer ? '正常在线 (Online)' : '等待连接 (Offline)'}
          </Text>
          <Text style={styles.textLine}>
            控制平台: {this.state.serverInfo.platform || 'Windows'}
          </Text>
          <Text style={styles.textLine}>
            协议版本: {this.state.serverInfo.version || 'v1.0.0'}
          </Text>
        </View>

        <Text style={styles.footerText}>PC 多媒体局域网远程遥控器</Text>

      </ScrollView>
    );
  }
});

AppRegistry.registerComponent('naiveClient', () => naiveClient);

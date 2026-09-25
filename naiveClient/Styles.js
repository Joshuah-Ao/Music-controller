'use strict';

var React = require('react-native');
var {
  StyleSheet,
} = React;

export const styles = StyleSheet.create({
  scrollView: {
    flex: 1,
    backgroundColor: '#0F172A',
  },
  container: {
    flex: 1,
    alignItems: 'stretch',
    backgroundColor: '#0F172A',
    padding: 24,
  },
  headerRow: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    marginVertical: 16,
    paddingBottom: 12,
    borderBottomWidth: 1,
    borderBottomColor: 'rgba(255, 255, 255, 0.1)',
  },
  row: {
    flexDirection: 'row',
    marginVertical: 12,
    alignItems: 'center',
    justifyContent: 'center',
  },
  textLine: {
    fontSize: 13,
    color: '#94A3B8',
    marginVertical: 3,
  },
  col: {
    flex: 1,
    flexDirection: 'column',
    alignItems: 'center',
    marginHorizontal: 8,
  },
  colNoAlign: {
    flex: 1,
    flexDirection: 'column',
    marginHorizontal: 8,
  },
  welcome: {
    fontSize: 22,
    fontWeight: 'bold',
    color: '#F8FAFC',
    letterSpacing: 0.5,
  },
  connectedBadge: {
    paddingHorizontal: 10,
    paddingVertical: 4,
    borderRadius: 12,
    backgroundColor: 'rgba(16, 185, 129, 0.2)',
  },
  connectedText: {
    color: '#34D399',
    fontSize: 12,
    fontWeight: 'bold',
  },
  controllerButton: {
    flex: 1,
    height: 52,
    borderRadius: 14,
    justifyContent: 'center',
    alignItems: 'center',
    elevation: 3,
  },
  playPauseButton: {
    flex: 1,
    height: 64,
    borderRadius: 18,
    marginHorizontal: 12,
    justifyContent: 'center',
    alignItems: 'center',
    elevation: 6,
  },
  textfieldWithFloatingLabel: {
    height: 48,
  },
  buttonText: {
    fontSize: 15,
    fontWeight: 'bold',
    color: '#FFFFFF',
  },
  playButtonText: {
    fontSize: 18,
    fontWeight: 'bold',
    color: '#FFFFFF',
  },
  card: {
    backgroundColor: 'rgba(30, 41, 59, 0.7)',
    borderRadius: 16,
    padding: 16,
    marginVertical: 14,
    borderWidth: 1,
    borderColor: 'rgba(255, 255, 255, 0.08)',
  },
  cardTitle: {
    fontSize: 14,
    fontWeight: 'bold',
    color: '#E2E8F0',
    marginBottom: 8,
  },
  footerText: {
    color: '#64748B',
    fontSize: 12,
    textAlign: 'center',
    marginTop: 20,
  }
});